import os
import json
import sqlite3
import time
import warnings
import pandas as pd
import yfinance as yf
import streamlit as st

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

st.title('📈 Portfolio Performance & Valuation Dashboard')
st.write('Upload your broker holdings sheet below to parse active trends instantly.')
uploaded_file = st.file_uploader('Choose your Zerodha holdings.csv file', type='csv')

if uploaded_file is not None:
    holdings_df = pd.read_csv(uploaded_file)
    holdings_df.columns = holdings_df.columns.str.strip()
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
        quantity = int(row['Qty.'])
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
                'h_price': h_price, 'h_date': h_date, 'l_price': l_price, 'l_date': l_date, 'current_value': current_value
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
        st.subheader('📋 Active Stock Position Grid Metrics')
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
        st.subheader('📊 Individual Asset Peak Analytics (Highs & Lows)')
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
            high_low_data.append({
                'Instrument': item['instrument'], 'Qty Held': item['qty'], 'Yearly High Price': item['h_price'], 'High Date': item['h_date'],
                'Yearly Low Price': item['l_price'], 'Low Date': item['l_date'], 'Max Potential Value (At High)': max_val, 'Min Potential Value (At Low)': min_val,
                'Current Value': curr_val, 'Downside': down_val, 'Opportunity': opp_val
            })
        hl_df = pd.DataFrame(high_low_data)
        st.dataframe(hl_df, use_container_width=True, hide_index=True, column_config={
            'Yearly High Price': st.column_config.NumberColumn(format='₹%,.2f'), 'Yearly Low Price': st.column_config.NumberColumn(format='₹%,.2f'),
            'Max Potential Value (At High)': st.column_config.NumberColumn(format='₹%,.2f'), 'Min Potential Value (At Low)': st.column_config.NumberColumn(format='₹%,.2f'),
            'Current Value': st.column_config.NumberColumn(format='₹%,.2f'), 'Downside': st.column_config.NumberColumn(format='₹%,.2f'),
            'Opportunity': st.column_config.NumberColumn(format='₹%,.2f')
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

        st.markdown('---')
        st.subheader('📊 Portfolio Strategic Summary Overview')
        col1, col2, col3, col4 = st.columns(4)
        col1.metric(label='Total Invested Base', value=format_currency_indian(total_invested_calc))
        col2.metric(label='Current Holding Value', value=format_currency_indian(total_current_calc))
        col3.metric(label='Total 1D Portfolio Shift', value=format_currency_indian(total_1d_change_calc), delta=f'{total_1d_change_calc:+,.2f}')
        col4.metric(label='Net Unrealized Returns', value=format_currency_indian(total_unrealized_calc), delta=f'{total_unrealized_calc:+,.2f}')