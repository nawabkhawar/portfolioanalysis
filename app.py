import streamlit as st
import pandas as pd
import yfinance as yf
import json
import sqlite3
import time
import os
import warnings

# Mute network warning layouts
warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

st.set_page_config(page_title="Portfolio Intelligence Analytics", layout="wide", initial_sidebar_state="expanded")

DB_FILE = "portfolio_cloud_cache.db"

def init_db():
    """Builds embedded SQLite infrastructure on the cloud container."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historical_prices (
            instrument_name TEXT, trade_date TEXT, open_price REAL,
            high_price REAL, lib_price REAL, low_price REAL, close_price REAL, volume INTEGER,
            PRIMARY KEY (instrument_name, trade_date)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS instrument_directory (
            zerodha_code TEXT PRIMARY KEY, yahoo_ticker TEXT, clean_db_name TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def resolve_ticker(zerodha_code):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT yahoo_ticker, clean_db_name FROM instrument_directory WHERE zerodha_code = ?", (zerodha_code,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return row['yahoo_ticker'], row['clean_db_name']
    
    if "NIFTY" in zerodha_code.upper():
        yf_ticker, clean_name = "^NSEI", "Nifty 50 Index"
    else:
        yf_ticker, clean_name = f"{zerodha_code}.NS", zerodha_code.strip().title()
        
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO instrument_directory (zerodha_code, yahoo_ticker, clean_db_name) VALUES (?, ?, ?)", (zerodha_code, yf_ticker, clean_name))
    conn.commit()
    conn.close()
    return yf_ticker, clean_name

def sync_yfinance_data(ticker, db_name):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        data = yf.download(ticker, period="1y", interval="1d", auto_adjust=True, multi_level_index=False)
        if not data.empty:
            data_sql = data.reset_index()
            data_sql['Date'] = data_sql['Date'].dt.strftime('%Y-%m-%d')
            records = []
            for _, r in data_sql.iterrows():
                records.append((db_name, r['Date'], float(r['Open']), float(r['High']), float(r['Low']), float(r['Close']), int(r['Volume'])))
            cursor.executemany("INSERT OR REPLACE INTO historical_prices (instrument_name, trade_date, open_price, high_price, low_price, close_price, volume) VALUES (?, ?, ?, ?, ?, ?, ?)", records)
            conn.commit()
    except Exception as e:
        st.warning(f"Throttled connection loop for {ticker}: {e}")
    finally:
        conn.close()

# --- Responsive UI Frontend Layout ---
st.title("📈 Portfolio Performance & Valuation Dashboard")
st.write("Upload your broker holdings sheet below to parse active trends instantly.")

uploaded_file = st.file_uploader("Choose your Zerodha holdings.csv file", type="csv")

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
    
    for idx, row in holdings_df.iterrows():
        raw_instrument = str(row['Instrument']).strip()
        quantity = int(row['Qty.'])
        avg_cost = float(row['Avg. cost'])
        
        yf_ticker, db_name = resolve_ticker(raw_instrument)
        
        # Check current cache
        cursor.execute("SELECT trade_date, high_price FROM historical_prices WHERE instrument_name = ? ORDER BY high_price DESC LIMIT 1", (db_name,))
        max_row = cursor.fetchone()
        cursor.execute("SELECT trade_date, low_price FROM historical_prices WHERE instrument_name = ? ORDER BY low_price ASC LIMIT 1", (db_name,))
        min_row = cursor.fetchone()
        
        if not max_row or not min_row:
            sync_yfinance_data(yf_ticker, db_name)
            cursor.execute("SELECT trade_date, high_price FROM historical_prices WHERE instrument_name = ? ORDER BY high_price DESC LIMIT 1", (db_name,))
            max_row = cursor.fetchone()
            cursor.execute("SELECT trade_date, low_price FROM historical_prices WHERE instrument_name = ? ORDER BY low_price ASC LIMIT 1", (db_name,))
            min_row = cursor.fetchone()
            
        if max_row and min_row:
            h_price, h_date = max_row['high_price'], max_row['trade_date']
            l_price, l_date = min_row['low_price'], min_row['trade_date']
            
            total_invested = quantity * avg_cost
            max_profit = (quantity * h_price) - total_invested
            max_loss = (quantity * l_price) - total_invested
            
            analysis_storage.append({
                "instrument": db_name, "qty": quantity, "avg_cost": avg_cost, "invested": total_invested,
                "h_price": h_price, "h_date": h_date, "max_profit": max_profit,
                "l_price": l_price, "l_date": l_date, "max_loss": max_loss
            })
            
            cursor.execute("SELECT trade_date, close_price FROM historical_prices WHERE instrument_name = ? ORDER BY trade_date ASC", (db_name,))
            time_rows = cursor.fetchall()
            series_tracker[db_name] = pd.Series([r['close_price'] for r in time_rows], index=pd.to_datetime([r['trade_date'] for r in time_rows]))
            
        progress_bar.progress((idx + 1) / total_rows)
        
    conn.close()
    
    if analysis_storage and series_tracker:
        # Build Pricing Alignment Pipelines
        price_matrix = pd.DataFrame(series_tracker).sort_index().ffill().bfill()
        
        portfolio_timeline = pd.Series(0.0, index=price_matrix.index)
        for item in analysis_storage:
            name = item['instrument']
            if name in price_matrix.columns:
                portfolio_timeline += price_matrix[name] * item['qty']
                
        # --- GRAPH 1: Net Worth Area Chart ---
        st.subheader("📊 Total Portfolio Valuation Trajectory (1Y)")
        st.area_chart(portfolio_timeline, use_container_width=True)
        
        # --- GRAPH 2: Overlay Multi-Line Price Chart ---
        st.subheader("📉 Stocks Historical Trends Matrix")
        st.line_chart(price_matrix, use_container_width=True)
        
        # --- SECTION 3: Performance Metric Summary Cards ---
        st.subheader("📋 Holding Details Summary Breakdown")
        cols = st.columns(len(analysis_storage)) if len(analysis_storage) <= 3 else st.columns(3)
        
        for idx, item in enumerate(analysis_storage):
            col_target = cols[idx % len(cols)]
            with col_target:
                with st.container(border=True):
                    st.markdown(f"### 🗂️ {item['instrument']}")
                    st.write(f"**Invested:** Rs. {item['invested']:,.2f} ({item['qty']} shares)")
                    st.success(f"🚀 **Peak High ({item['h_date']}):** Rs. {item['h_price']:,.2f}\n\nMax Profit: **Rs. {item['max_profit']:,.2f}**")
                    st.error(f"📉 **Floor Low ({item['l_date']}):** Rs. {item['l_price']:,.2f}\n\nMax Loss: **Rs. {item['max_loss']:,.2f}**")
