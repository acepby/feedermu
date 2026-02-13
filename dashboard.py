import streamlit as st
import sqlite3
import pandas as pd
import requests
import feedparser
import schedule
import time
import threading
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
import re
from collections import Counter

# --- CONFIGURATION ---
DB_NAME = "web_monitor_v2.db"
HEADERS = {'User-Agent': 'Mozilla/5.0'}

# --- BACKEND: DATABASE ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. Targets Table (Institutions)
    c.execute('''CREATE TABLE IF NOT EXISTS targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        institution TEXT,
        domain TEXT UNIQUE,
        lat REAL,
        lon REAL,
        rss_flag INTEGER DEFAULT 0,
        rss_url TEXT,
        last_checked TEXT
    )''')
    
    # 2. Articles Table (Individual Items)
    c.execute('''CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id INTEGER,
        title TEXT,
        url TEXT UNIQUE,
        published_date TEXT,
        found_at TEXT,
        FOREIGN KEY(target_id) REFERENCES targets(id)
    )''')
    
    conn.commit()
    conn.close()

def add_target(institution, domain, lat, lon):
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT INTO targets (institution, domain, lat, lon) VALUES (?, ?, ?, ?)", 
                  (institution, domain, lat, lon))
        conn.commit()
        conn.close()
        return True, "Success"
    except Exception as e:
        return False, str(e)

def save_article(target_id, title, url, pub_date):
    """Saves an individual article. Ignores duplicates."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        # FIX: Explicit string conversion for Python 3.12+
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        c.execute('''INSERT OR IGNORE INTO articles 
                     (target_id, title, url, published_date, found_at) 
                     VALUES (?, ?, ?, ?, ?)''', 
                  (target_id, title, url, pub_date, current_time))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error saving article: {e}")

def get_targets():
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT * FROM targets", conn)
    conn.close()
    return df

def get_articles(target_id=None):
    conn = sqlite3.connect(DB_NAME)
    query = """
        SELECT a.id, t.institution, a.title, a.url, a.published_date, a.found_at 
        FROM articles a
        JOIN targets t ON a.target_id = t.id
    """
    if target_id:
        query += f" WHERE t.id = {target_id}"
    
    query += " ORDER BY a.found_at DESC LIMIT 50"
    
    # FIX: Parse dates so charts work correctly
    df = pd.read_sql_query(query, conn, parse_dates=['found_at'])
    conn.close()
    return df

def get_trending_topics():
    conn = sqlite3.connect(DB_NAME)
    # Get all titles from the last 7 days
    query = """
        SELECT title FROM articles 
        WHERE found_at >= date('now', '-7 days')
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        return {}

    # Combine all titles into one big text
    text = " ".join(df['title'].astype(str).tolist()).lower()

    # 1. Remove all numbers
    text = re.sub(r'\d+', '', text)
    
    # 2. Remove punctuation/symbols (keep only letters)
    text = re.sub(r'[^\w\s]', '', text)
    
    # Split into words
    words = text.split()
    
    # STOP WORDS (Common words to ignore)
    stop_words = set([
        # English
        'the', 'a', 'an', 'and', 'to', 'of', 'in', 'is', 'for', 'on', 'with', 'at', 'by', 'from',
        # Indonesian
        'dan', 'yang', 'di', 'ini', 'itu', 'dari', 'ke', 'untuk', 'pada', 'adalah', 'dengan','tahun', 
        'sebagai', 'tidak', 'akan', 'juga', 'oleh', 'sudah', 'atau', 'karena', 'lpcr', 'pp', 'muhammadiyah'
    ])
    
    # Filter out stop words and short words
    meaningful_words = [w for w in words if w not in stop_words and len(w) > 3]
    
    # Count frequency
    return dict(Counter(meaningful_words).most_common(10))

def get_daily_stats():
    conn = sqlite3.connect(DB_NAME)
    # Groups articles by Date and Counts them
    query = """
        SELECT date(found_at) as date, count(*) as count 
        FROM articles 
        GROUP BY date(found_at) 
        ORDER BY date ASC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def delete_target(id):
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM articles WHERE target_id = ?", (id,))
        c.execute("DELETE FROM targets WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        return True
    except: return False

def edit_target_details(id, new_inst, new_dom, new_lat, new_lon):
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE targets SET institution=?, domain=?, lat=?, lon=? WHERE id=?", 
                  (new_inst, new_dom, new_lat, new_lon, id))
        conn.commit()
        conn.close()
        return True, "Updated"
    except Exception as e: return False, str(e)

# --- BACKEND: CRAWLER ---
def find_rss_url(soup, base_url):
    link = soup.find('link', type='application/rss+xml')
    if link and link.get('href'): return link.get('href')
    common = ['/feed/', '/rss/', '/rss.xml', '/blog/feed/','/rss/latest-posts']
    for path in common:
        try:
            full = urljoin(base_url, path)
            if requests.head(full, headers=HEADERS, timeout=2).status_code == 200: return full
        except: continue
    return None

def scan_domain(target_row):
    domain = target_row['domain']
    target_id = target_row['id']
    try:
        response = requests.get(domain, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        rss_url = find_rss_url(soup, domain)
        rss_flag = 0
        
        if rss_url:
            feed = feedparser.parse(rss_url)
            if not feed.bozo and len(feed.entries) > 0:
                rss_flag = 1
                for entry in feed.entries:
                    # Ensure published date is a string
                    pub = entry.get('published', datetime.now().strftime("%Y-%m-%d"))
                    save_article(target_id, entry.title, entry.link, pub)
        
        if rss_flag == 0:
            headers = soup.find_all(['h1', 'h2'])
            for h in headers:
                text = h.get_text(strip=True)
                link_tag = h.find('a')
                url = link_tag['href'] if link_tag and link_tag.get('href') else domain
                full_url = urljoin(domain, url)
                if text:
                    unique_ref = full_url if link_tag else f"{domain}#{text}"
                    save_article(target_id, text, unique_ref, "Scraped")

        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        # FIX: Explicit string conversion
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        c.execute("UPDATE targets SET rss_flag=?, rss_url=?, last_checked=? WHERE id=?", 
                  (rss_flag, rss_url, current_time, target_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error scanning {domain}: {e}")

def run_batch_job():
    print(f"⏳ Batch Job Starting: {datetime.now()}")
    df = get_targets()
    for _, row in df.iterrows():
        scan_domain(row)
    print("✅ Batch Job Finished")

# --- SCHEDULER (FIXED) ---
@st.cache_resource
def start_scheduler():
    def job():
        # 1. Schedule future runs
        schedule.every(30).minutes.do(run_batch_job)
        
        # 2. RUN IMMEDIATELY ON STARTUP
        run_batch_job()
        
        # 3. Loop
        while True:
            schedule.run_pending()
            time.sleep(1)
            
    # Start the background thread
    t = threading.Thread(target=job, daemon=True)
    t.start()
    return t

# --- FRONTEND ---
st.set_page_config(page_title="RSS Item Monitor", layout="wide")
init_db()
start_scheduler()

st.title("📰 Monitor Web Muhammadiyah")

# Sidebar
with st.sidebar:
    st.header("Add Target")
    with st.form("add"):
        inst = st.text_input("Institution")
        dom = st.text_input("Domain")
        c1, c2 = st.columns(2)
        lat = c1.number_input("Lat", -7.8, format="%.6f")
        lon = c2.number_input("Lon", 110.3, format="%.6f")
        if st.form_submit_button("Add"):
            if inst and dom:
                ok, msg = add_target(inst, dom, lat, lon)
                if ok: 
                    st.success("Added!")
                    st.rerun()
                else: st.error(msg)

# Tabs
t1, t2, t3, t4 = st.tabs(["📄 All Articles", "🏢 Institutions", "🗺️ Map", "🛠️ Manage"])

# TAB 1: ALL INDIVIDUAL ARTICLES
with t1:
    # --- TOPICS ANALYSIS ---
    st.subheader("🔥 Trending Topics (Last 7 Days)")
    topics = get_trending_topics()
    
    if topics:
        topic_df = pd.DataFrame(list(topics.items()), columns=['Keyword', 'Count'])
        topic_df = topic_df.sort_values(by='Count', ascending=True)
        st.bar_chart(topic_df.set_index('Keyword'), color="#FF4B4B", horizontal=True)
    else:
        st.caption("Not enough data to determine trending topics yet.")

    st.divider()

    # --- CHART SECTION ---
    st.subheader("📈 Daily Articles Collected")
    daily_df = get_daily_stats()
    if not daily_df.empty:
        st.bar_chart(daily_df.set_index('date'))
    else:
        st.caption("No data to chart yet.")
    
    st.divider()

    # --- TABLE SECTION ---
    st.subheader("Latest Stream")
    
    targets = get_targets()
    if not targets.empty:
        target_map = {f"{row['institution']}": row['id'] for _, row in targets.iterrows()}
        options = ["All"] + list(target_map.keys())
        selected_option = st.selectbox("Filter by Institution:", options)
        
        selected_id = None
        if selected_option != "All":
            selected_id = target_map[selected_option]

        if st.button("🔄 Refresh Data"):
            with st.spinner("Fetching updates..."):
                run_batch_job()
            st.rerun()

        articles = get_articles(selected_id)
        if not articles.empty:
            st.dataframe(
                articles,
                column_config={
                    "url": st.column_config.LinkColumn("Link"),
                    "found_at": st.column_config.DatetimeColumn("Detected At", format="D MMM HH:mm")
                },
                width='stretch',
                hide_index=True
            )
        else:
            st.info("No articles found yet.")
    else:
        st.info("Add a target to start.")

# TAB 2: INSTITUTIONS STATUS
with t2:
    st.subheader("Target Status")
    df = get_targets()
    if not df.empty:
        df['rss_flag'] = df['rss_flag'].apply(lambda x: "✅" if x==1 else "❌")
        st.dataframe(df[['institution', 'domain', 'rss_flag', 'last_checked']], width='stretch')

# TAB 3: MAP
with t3:
    st.subheader("📍 Geospatial Map")
    df = get_targets()
    
    if not df.empty:
        try:
            # 1. Ensure coordinates are Numbers (Floats), not Strings
            df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
            df['lon'] = pd.to_numeric(df['lon'], errors='coerce')
            
            # 2. Drop rows where lat/lon is missing/empty
            map_data = df.dropna(subset=['lat', 'lon'])

            # 3. Rename columns to what Streamlit explicitly wants
            map_data = map_data.rename(columns={'lat': 'latitude', 'lon': 'longitude'})

            if not map_data.empty:
                # Display the map
                st.map(map_data, zoom=10)
            else:
                st.warning("Institutions exist in database, but they have invalid or missing Latitude/Longitude.")
        except Exception as e:
            st.error(f"Error displaying map: {e}")
    else:
        st.info("No data found. Please add an institution in the Sidebar.")

# TAB 4: MANAGE
with t4:
    df = get_targets()
    if not df.empty:
        opts = {f"{r['id']} - {r['institution']}": r['id'] for _, r in df.iterrows()}
        sel = st.selectbox("Edit/Delete Target:", list(opts.keys()))
        s_id = opts[sel]
        curr = df[df['id'] == s_id].iloc[0]
        
        c_edit, c_del = st.columns([2,1])
        with c_edit:
            with st.form("edit_f"):
                ni = st.text_input("Name", curr['institution'])
                nd = st.text_input("Domain", curr['domain'])
                c1, c2 = st.columns(2)
                nl = c1.number_input("Lat", value=curr['lat'])
                nlo = c2.number_input("Lon", value=curr['lon'])
                if st.form_submit_button("Update"):
                    edit_target_details(s_id, ni, nd, nl, nlo)
                    st.rerun()
        with c_del:
            st.write("Danger Zone")
            if st.button("Delete Target & Articles"):
                delete_target(s_id)
                st.rerun()
