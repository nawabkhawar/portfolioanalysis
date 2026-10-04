import os
import io
import json
import sqlite3
import time
import warnings
import pandas as pd
import yfinance as yf
import streamlit as st

# Mute network warning layouts from console outputs safely by passing class
warnings.filterwarnings('ignore', category=UserWarning, module='urllib3')

st.set_page_config(page_title='Portfolio Intelligence Analytics', layout='wide', initial_sidebar_state='expanded')

DB_FILE = 'portfolio_cloud_cache.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS historical_prices (
            instrument_name TEXT, trade_date TEXT, open_price REAL,
            high_price REAL, low_price REAL, close_price REAL, volume INTEGER,
            PRIMARY KEY (instrument_name, trade_date)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS instrument_directory (
            zerodha_code TEXT PRIMARY KEY, yahoo_ticker TEXT, clean_db_name TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def resolve_ticker(zerodha_code):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT yahoo_ticker, clean_db_name FROM instrument_directory WHERE zerodha_code = ?', (zerodha_code,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row['yahoo_ticker'], row['clean_db_name']
    if 'NIFTY' in zerodha_code.upper():
        yf_ticker, clean_name = '^NSEI', 'Nifty 50 Index'
    else:
        yf_ticker, clean_name = f'{zerodha_code}.NS', zerodha_code.strip().title()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO instrument_directory (zerodha_code, yahoo_ticker, clean_db_name) VALUES (?, ?, ?)', (zerodha_code, yf_ticker, clean_name))
    conn.commit()
    conn.close()
    return yf_ticker, clean_name

def sync_yfinance_data(ticker, db_name):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        data = yf.download(ticker, period='1y', interval='1d', auto_adjust=True, multi_level_index=False)
        if not data.empty:
            data_sql = data.reset_index()
            data_sql['Date'] = data_sql['Date'].dt.strftime('%Y-%m-%d')
            records = []
            for _, r in data_sql.iterrows():
                records.append((db_name, r['Date'], float(r['Open']), float(r['High']), float(r['Low']), float(r['Close']), int(r['Volume'])))
            cursor.executemany('INSERT OR REPLACE INTO historical_prices (instrument_name, trade_date, open_price, high_price, low_price, close_price, volume) VALUES (?, ?, ?, ?, ?, ?, ?)', records)
            conn.commit()
    except Exception as e:
        st.warning(f'Throttled connection loop for {ticker}: {e}')
    finally:
        conn.close()

def format_currency_indian(val):
    if abs(val) >= 10000000:
        return f'₹{val / 10000000:.2f} Cr'
    elif abs(val) >= 100000:
        return f'₹{val / 100000:.2f} L'
    else:
        return f'₹{val:,.2f}'

def parse_flexible_portfolio(file_obj, file_name):
    """
    Intelligently reads file bytes to accept standard Zerodha CSV columns,
    or handles multi-section statement layouts seamlessly.
    """
    try:
        if file_name.endswith('.xlsx') or file_name.endswith('.xls'):
            df_raw = pd.read_excel(file_obj, header=None)
        else:
            content = file_obj.read()
            if isinstance(content, bytes):
                content = content.decode('utf-8', errors='ignore')
            df_raw = pd.read_csv(io.StringIO(content), header=None)
            
        # Check if this matches a complex multi-section statement format
        df_str = df_raw.astype(str)
        is_statement = df_str.apply(lambda row: row.str.contains('Holdings Statement|Investment Value', case=False).any(), axis=1).any()
        
        if is_statement:
            # Locate the Equity section rows
            equity_header_idx = None
            for idx, row in df_raw.iterrows():
                row_vals = [str(x).strip().lower() for x in row.dropna()]
                if 'symbol' in row_vals and 'quantity available' in row_vals:
                    equity_header_idx = idx
                    break
            
            if equity_header_idx is not None:
                # Process the data slice under the isolated table headers
                df_clean = df_raw.iloc[equity_header_idx:].copy()
                df_clean.columns = df_clean.iloc[0].str.strip()
                df_clean = df_clean.iloc[1:].reset_index(drop=True)
                df_clean = df_clean.dropna(subset=['Symbol', 'Quantity Available'])
                
                # Filter out subsequent mutual fund headers or spacer blank sections
                stop_idx = None
                for idx, row_val in enumerate(df_clean['Symbol'].astype(str)):
                    if 'mutual funds' in row_val.lower() or 'client id' in row_val.lower() or row_val.strip() == '':
                        stop_idx = idx
                        break
                if stop_idx is not None:
                    df_clean = df_clean.iloc[:stop_idx]
                
                # Harmonize header column definitions to standard Zerodha formatting blueprint
                df_mapped = pd.DataFrame()
                df_mapped['Instrument'] = df_clean['Symbol'].astype(str).str.strip()
                df_mapped['Qty.'] = pd.to_numeric(df_clean['Quantity Available'], errors='coerce')
                df_mapped['Avg. cost'] = pd.to_numeric(df_clean['Average Price'], errors='coerce')
                
                return df_mapped.dropna().reset_index(drop=True)
                
        # Fallback Default: Parse as a standard, straightforward Zerodha layout
        if 'seek' in dir(file_obj):
            file_obj.seek(0)
        if file_name.endswith('.xlsx') or file_name.endswith('.xls'):
            df_standard = pd.read_excel(file_obj)
        else:
            df_standard = pd.read_csv(file_obj)
            
        df_standard.columns = df_standard.columns.str.strip()
        required = ['Instrument', 'Qty.', 'Avg. cost']
        if all(col in df_standard.columns for col in required):
            return df_standard[required].copy()
        else:
            # Try to map columns if slightly different casing/naming was used
            mapped_df = pd.DataFrame()
            for col in df_standard.columns:
                c_clean = str(col).strip().lower()
                if c_clean in ['instrument', 'symbol', 'shares']:
                    mapped_df['Instrument'] = df_standard[col]
                elif c_clean in ['qty.', 'qty', 'quantity', 'quantity available']:
                    mapped_df['Qty.'] = df_standard[col]
                elif c_clean in ['avg. cost', 'avg cost', 'average price', 'buy price']:
                    mapped_df['Avg. cost'] = df_standard[col]
            if len(mapped_df.columns) == 3:
                return mapped_df
                
        st.error("❌ Column Layout Error: Uploaded file formatting columns could not be successfully resolved.")
        return None
    except Exception as e:
        st.error(f"❌ File Parsing Exception Encountered: {e}")
        return None

st.title('📈 Nawabs Portfolio Performance & Valuation Dashboard - No data is saved, its all in your laptop/mobile')
st.write('Upload your broker holdings sheet below (supports standard Zerodha CSVs or Excel Statements) to parse active trends instantly.')

# File Uploader component updated to accept both target variants seamlessly
uploaded_file = st.file_uploader('Choose your broker holdings file (.csv or .xlsx)', type=['csv', 'xlsx', 'xls'])

# Dynamic fallback raw string used internally for the demo engine
fallback_csv_data = (
    "Instrument,Qty.,Avg. cost\n"
    "APOLLOTYRE,255,321.70\n"
    "DELHIVERY,110,409.09\n"
    "INFY,5,1438.91\n"
    "LALPATHLAB,20,1514.87\n"
    "RELIANCE,74,426.69\n"
    "WIPRO,151,195.92"
)

# Render interactive Demo Button right under file picker element
trigger_demo = st.button("✨ Try Demo with Sample Data", help="Instantly calculate analytics using demo metrics without manually downloading files")

# Data processing fork selector
holdings_df = None
is_demo_mode = False

if uploaded_file is not None:
    holdings_df = parse_flexible_portfolio(uploaded_file, uploaded_file.name)
    is_demo_mode = False
elif trigger_demo:
    # Read embedded fallback layout context directly into pandas memory buffer
    holdings_df = pd.read_csv(io.StringIO(fallback_csv_data))
    is_demo_mode = True

if holdings_df is not None and not holdings_df.empty:
    # Visual validation block running strictly under sample demo selections
    if is_demo_mode:
        st.success("🎯 App is currently running on simulated demo portfolio metrics data!(Zerodha holdings file example as below)")
        with st.expander("📝 View Zerodha holding file used for this example", expanded=True):
            st.dataframe(holdings_df, use_container_width=True)

    analysis_storage = []
    series_tracker = {}
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    progress_bar = st.progress(0)
    total_rows = len(holdings_df)
    total_invested_calc = 0.0
    total_current_calc = 0.0
    total_1d_change_calc = 0.0
    total_unrealized_calc = 0.0
    raw_table_rows = []

    for idx, row in holdings_df.iterrows():
        raw_instrument = str(row['Instrument']).strip()
        quantity = int(float(row['Qty.']))
        avg_cost = float(row['Avg. cost'])
        yf_ticker, db_name = resolve_ticker(raw_instrument)
        cursor.execute('SELECT trade_date, high_price FROM historical_prices WHERE instrument_name = ? ORDER BY high_price DESC LIMIT 1', (db_name,))
        max_row = cursor.fetchone()
        cursor.execute('SELECT trade_date, low_price FROM historical_prices WHERE instrument_name = ? ORDER BY low_price ASC LIMIT 1', (db_name,))
        min_row = cursor.fetchone()
        if not max_row or not min_row:
            sync_yfinance_data(yf_ticker, db_name)
            cursor.execute('SELECT trade_date, high_price FROM historical_prices WHERE instrument_name = ? ORDER BY high_price DESC LIMIT 1', (db_name,))
            max_row = cursor.fetchone()
            cursor.execute('SELECT trade_date, low_price FROM historical_prices WHERE instrument_name = ? ORDER BY low_price ASC LIMIT 1', (db_name,))
            min_row = cursor.fetchone()
        if max_row and min_row:
            h_price, h_date = max_row['high_price'], max_row['trade_date']
            l_price, l_date = min_row['low_price'], min_row['trade_date']
            cursor.execute('SELECT trade_date, close_price FROM historical_prices WHERE instrument_name = ? ORDER BY trade_date DESC LIMIT 2', (db_name,))
            latest_candles = cursor.fetchall()
            ltp = float(latest_candles[0]['close_price']) if len(latest_candles) > 0 else avg_cost
            prev_close = float(latest_candles[1]['close_price']) if len(latest_candles) > 1 else ltp
            invested_amount = quantity * avg_cost
            current_value = quantity * ltp
            one_day_change = quantity * (ltp - prev_close)
            unrealized_pnl = current_value - invested_amount
            total_invested_calc += invested_amount
            total_current_calc += current_value
            total_1d_change_calc += one_day_change
            total_unrealized_calc += unrealized_pnl
            analysis_storage.append({
                'instrument': db_name, 'qty': quantity, 'avg_cost': avg_cost, 'invested': invested_amount,
                'h_price': h_price, 'h_date': h_date, 'l_price': l_price, 'l_date': l_date, 'current_value': current_value, 'current_price': ltp
            })
            raw_table_rows.append({
                'Company': db_name, 'Qty': quantity, 'Avg. Cost': avg_cost, 'LTP': ltp,
                'Inv. Amount': invested_amount, 'Curr. Value': current_value, '1D Chg.': one_day_change, 'Unrealized P&L': unrealized_pnl
            })
            cursor.execute('SELECT trade_date, close_price FROM historical_prices WHERE instrument_name = ? ORDER BY trade_date ASC', (db_name,))
            time_rows = cursor.fetchall()
            series_tracker[db_name] = pd.Series([r['close_price'] for r in time_rows], index=pd.to_datetime([r['trade_date'] for r in time_rows]))
        progress_bar.progress((idx + 1) / total_rows)
    conn.close()

    if raw_table_rows:
        # --- PORTFOLIO STRATEGIC SUMMARY OVERVIEW (TOP LEVEL) ---
        st.subheader('📊 Portfolio Strategic Summary Overview')
        col1, col2, col3, col4 = st.columns(4)
        col1.metric(label='Total Invested Base', value=format_currency_indian(total_invested_calc))
        col2.metric(label='Current Holding Value', value=format_currency_indian(total_current_calc))
        col3.metric(label='Total 1D Portfolio Shift', value=format_currency_indian(total_1d_change_calc), delta=f'{total_1d_change_calc:+,.2f}')
        col4.metric(label='Net Unrealized Returns', value=format_currency_indian(total_unrealized_calc), delta=f'{total_unrealized_calc:+,.2f}')
        
        st.markdown('---')
        
        # --- ACTIVE POSITION BREAKDOWN GRID MATRIX ---
        st.subheader('📋 todays/last trading session - profit & loss per share analysis')
        st.info('💡 Pro-Tip: Click directly on any column header to sort it instantly in Ascending or Descending order!')
        final_table_data = []
        for r in raw_table_rows:
            weight = (r['Inv. Amount'] / total_invested_calc) * 100 if total_invested_calc > 0 else 0
            final_table_data.append({
                'Company': r['Company'], 'Weights': round(weight, 2), 'Qty': r['Qty'], 'Avg. Cost': r['Avg. Cost'],
                'LTP': r['LTP'], 'Inv. Amount': r['Inv. Amount'], 'Curr. Value': r['Curr. Value'], '1D Chg.': r['1D Chg.'], 'Unrealized P&L': r['Unrealized P&L']
            })
        grid_df = pd.DataFrame(final_table_data)
        st.dataframe(grid_df, use_container_width=True, hide_index=True, column_config={
            'Company': st.column_config.TextColumn('Company'), 'Weights': st.column_config.NumberColumn('WEIGHTS', format='%.2f%%'),
            'Qty': st.column_config.NumberColumn('Qty'), 'Avg. Cost': st.column_config.NumberColumn('Avg. Cost', format='₹%.2f'),
            'LTP': st.column_config.NumberColumn('LTP', format='₹%.2f'), 'Inv. Amount': st.column_config.NumberColumn('Inv. Amount', format='₹%,.2f'),
            'Curr. Value': st.column_config.NumberColumn('Curr. Value', format='₹%,.2f'), '1D Chg.': st.column_config.NumberColumn('1D Chg.', format='₹%,.2f'),
            'Unrealized P&L': st.column_config.NumberColumn('Unrealized P&L', format='₹%,.2f'),
        })

        st.markdown('---')
        st.subheader('📊 Individual Asset Peak Analytics (Highs & Lows - when your share went highest & lowest in last 1 year & what was the profit/loss)')
        high_low_data = []
        sum_max_potential = 0.0
        sum_min_potential = 0.0
        sum_current_val = 0.0
        sum_opportunity = 0.0
        sum_downside = 0.0
        for item in analysis_storage:
            max_val = item['qty'] * item['h_price']
            min_val = item['qty'] * item['l_price']
            curr_val = item['current_value']
            opp_val = max_val - curr_val
            down_val = min_val - curr_val
            sum_max_potential += max_val
            sum_min_potential += min_val
            sum_current_val += curr_val
            sum_opportunity += opp_val
            sum_downside += down_val
            
            # Formulating dictionary variables with the specified order:
            # Lowest Price after Highest Price, followed by adjacent High Date and Low Date
            high_low_data.append({
                'Instrument': item['instrument'], 
                'Qty Held': item['qty'], 
                'Current Price': item['current_price'],
                'Higest Price in last 1 yr': item['h_price'], 
                'Lowest Price in last 1 yr': item['l_price'], 
                'High Date': item['h_date'],
                'Low Date': item['l_date'], 
                'Total Value @High': max_val, 
                'Total Value @Low': min_val,
                'Current Total': curr_val, 
                'Opportunity': opp_val,
                'Downside': down_val
            })
        hl_df = pd.DataFrame(high_low_data)
        st.dataframe(hl_df, use_container_width=True, hide_index=True, column_config={
            'Current Price': st.column_config.NumberColumn(format='₹%,.2f'),
            'Higest Price in last 1 yr': st.column_config.NumberColumn(format='₹%,.2f'), 
            'Lowest Price in last 1 yr': st.column_config.NumberColumn(format='₹%,.2f'),
            'Total Value @High': st.column_config.NumberColumn(format='₹%,.2f'), 
            'Total Value @Low': st.column_config.NumberColumn(format='₹%,.2f'),
            'Current Total': st.column_config.NumberColumn(format='₹%,.2f'), 
            'Opportunity': st.column_config.NumberColumn(format='₹%,.2f'),
            'Downside': st.column_config.NumberColumn(format='₹%,.2f')
        })

        st.markdown('##### 📌 Peak Analytics Portfolio Aggregates')
        sum_col1, sum_col2, sum_col3, sum_col4, sum_col5 = st.columns(5)
        sum_col1.metric(label='Portfolio Max Bound (Highs)', value=format_currency_indian(sum_max_potential))
        sum_col2.metric(label='Portfolio Min Bound (Lows)', value=format_currency_indian(sum_min_potential))
        sum_col3.metric(label='Current Portfolio Value', value=format_currency_indian(sum_current_val))
        sum_col4.metric(label='Total Portfolio Downside', value=format_currency_indian(sum_downside))
        sum_col5.metric(label='Total Portfolio Opportunity', value=format_currency_indian(sum_opportunity))

        if series_tracker:
            st.markdown('---')
            st.subheader('📈 Historical Portfolio Growth & Trend Curves (1Y)')
            price_matrix = pd.DataFrame(series_tracker).sort_index().ffill().bfill()
            portfolio_timeline = pd.Series(0.0, index=price_matrix.index)
            for item in analysis_storage:
                name = item['instrument']
                if name in price_matrix.columns:
                    portfolio_timeline += price_matrix[name] * item['qty']
            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                st.markdown('**Total Portfolio Valuation Trajectory (Net Asset Value)**')
                st.area_chart(portfolio_timeline, use_container_width=True)
            with chart_col2:
                st.markdown('**Granular Individual Stocks Performance Trends**')
                st.line_chart(price_matrix, use_container_width=True)

        st.markdown('---')
        st.subheader('⚡ 7-Day Asset Momentum Analysis (Normalized %)')
        st.write('Plots the relative short-term performance trajectory of your instruments over the last 7 trading days normalized from a baseline of 0%.')
        if len(price_matrix) >= 7:
            recent_prices = price_matrix.tail(7).copy()
        else:
            recent_prices = price_matrix.copy()
        if not recent_prices.empty:
            normalized_momentum = (recent_prices / recent_prices.iloc[0] - 1.0) * 100.0
            st.line_chart(normalized_momentum, use_container_width=True)
        else:
            st.info('Insufficient historical price observations to isolate short-term momentum parameters.')
