#!/usr/bin/python3 -tt
# fastebaysearch 6.2.2026 16:30 alpha test version 20
# + Combined Search Header into the first Telegram find message.
# + Fixed NameError (raw_id) and parameter passing (rates).
# License: GPLv2

import json
import os
import smtplib
import sqlite3
import sys
import asyncio
import base64
import requests
import time
import re
import logging
import html
from urllib.parse import urlparse
from datetime import datetime, timezone
from dateutil import parser
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import aiohttp

class RateLimitError(Exception):
    pass

start_time = time.time()

# ------------------------------
# CONFIGURATION AND LOGS
# ------------------------------
LOG_FILE = "fastebaysearch.log"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
TOKEN_FILE = "oauth_token.json"

logger = logging.getLogger("fastebaysearch")
logger.setLevel(logging.DEBUG)
logger.propagate = False

if logger.handlers:
    logger.handlers.clear()

file_handler = logging.FileHandler(LOG_FILE, mode='a')
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))

try:
    with open("ebaysearch.json", "r") as f:
        log_config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    log_config = {"log_to_console": True}

if log_config.get("log_to_console", True):
    logger.addHandler(console_handler)

# ------------------------------
# HELPERS
# ------------------------------
def normalize_terms(lst):
    return [str(t).strip() for t in (lst or []) if t is not None and str(t).strip()]

def utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def clean_ebay_id(item_id):
    if not item_id: return None
    s = str(item_id)
    m = re.search(r"\|(\d+)\|", s)
    if m: return m.group(1)
    m = re.fullmatch(r"\d+", s)
    if m: return s
    m = re.search(r"\d{5,}", s)
    return m.group(0) if m else None

def format_date(date_str):
    if not date_str or date_str == "Not available": return "Not available"
    try:
        dt = parser.parse(date_str)
        return dt.strftime("%d.%m.%Y %H:%M:%S")
    except: return "Not available"
        
async def get_exchange_rates():
    url = "https://api.frankfurter.app/latest?base=EUR&symbols=USD,GBP,AUD"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rates = data.get("rates", {})
                    logger.info(f"✅ Valuuttakurssit päivitetty: {rates}")
                    return rates
    except Exception as e:
        logger.error(f"Valuuttakurssien haku epäonnistui: {e}")
    return {}

# ------------------------------
# DATABASE FUNCTIONS
# ------------------------------
def create_database_if_not_exists(db_path):
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ebayids';")
            if not cur.fetchone():
                conn.executescript("""
                    CREATE TABLE ebayids (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT UNIQUE NOT NULL, insert_time TEXT NOT NULL);
                    CREATE INDEX idx_item_id ON ebayids (item_id);
                """)
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='history';")
            if not cur.fetchone():
                conn.executescript("""
                    CREATE TABLE history (id INTEGER PRIMARY KEY, run_number INTEGER, start_time TEXT, end_time TEXT);
                    INSERT INTO history (id, run_number, start_time, end_time) VALUES (0, 0, NULL, NULL);
                """)
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"❌ Database error: {e}")
        sys.exit(1)

def db_history_new_run(db_path):
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(run_number) FROM history;")
            new_run = (cur.fetchone()[0] or 0) + 1
            conn.execute("INSERT INTO history (run_number, start_time, end_time) VALUES (?, NULL, NULL);", (new_run,))
            conn.commit()
            return new_run
    except: return None

def db_history_set_timestamp(db_path, run_number, timestamp_type):
    if run_number is None: return
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(f"UPDATE history SET {timestamp_type} = ? WHERE run_number = ?;", (utc_now_str(), int(run_number)))
    except: pass

def db_search_for_urls(db_path, item_ids):
    if not item_ids: return set()
    found = set()
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        for i in range(0, len(item_ids), 900):
            chunk = item_ids[i:i+900]
            cur.execute(f"SELECT item_id FROM ebayids WHERE item_id IN ({','.join(['?']*len(chunk))})", chunk)
            found.update(row[0] for row in cur.fetchall())
    return found

def db_insert_urls(db_path, item_ids):
    if not item_ids: return
    insert_time = utc_now_str()
    with sqlite3.connect(db_path) as conn:
        conn.executemany("INSERT OR IGNORE INTO ebayids (item_id, insert_time) VALUES (?, ?)", [(i, insert_time) for i in item_ids])

# ------------------------------
# CORE LOGIC
# ------------------------------
def parse_search_results(keywords, raw_items, rates):
    results = []
    for item in raw_items:
        # FIXED: raw_id määritelmä lisätty
        raw_id = item.get("itemId") or item.get("legacyItemId")
        item_id = clean_ebay_id(raw_id)
        if not item_id: continue

        price_data = item.get("price", {})
        val = float(price_data.get("value", 0))
        curr = price_data.get("currency", "EUR").upper()
        
        if curr == "EUR":
            display_price = f"{val:.2f} EUR"
        elif curr in rates:
            eur_val = val / rates[curr]
            display_price = f"{eur_val:.2f} EUR ({val:.2f} {curr})"
        else:
            display_price = f"{val:.2f} {curr}"

        results.append({
            "Keywords": str(keywords),
            "Name": item.get("title", "Unknown"),
            "Ebay-site": item.get("listingMarketplaceId", "Unknown"),
            "Price": display_price,
            "itemId": item_id,
            "Link": item.get("itemWebUrl", "").split("?")[0],
            "Seller": item.get("seller", {}).get("username", "Unknown"),
            "Starts": format_date(item.get("itemCreationDate")),
            "Ends": format_date(item.get("itemEndDate")),
        })
    return results

async def search_ebay(session, token, site, full_query):
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": site, "Accept": "application/json"}
    all_results = []
    offset = 0
    while True:
        params = {"q": full_query, "limit": "200", "offset": str(offset)}
        async with session.get("https://api.ebay.com/buy/browse/v1/item_summary/search", headers=headers, params=params) as resp:
            if resp.status == 429: raise RateLimitError()
            if resp.status != 200: break
            data = await resp.json()
            items = data.get("itemSummaries", [])
            all_results.extend(items)
            if int(data.get("total", 0)) <= offset + len(items) or len(items) < 200 or offset >= 1000: break
            offset += 200
    return all_results

async def run_all_searches(config, token, rates):
    all_parsed = []
    async with aiohttp.ClientSession() as session:
        tasks = []
        for site in config.get('ebay_sites', []):
            for search_item in config.get('ebay_search_keywords', []):
                tasks.append((search_item["base_name"], search_ebay(session, token, site, search_item["keywords"])))
        raw_results = await asyncio.gather(*(t[1] for t in tasks))
        for (base, _), raw_items in zip(tasks, raw_results):
            # FIXED: rates parametri lisätty
            all_parsed.extend(parse_search_results(base, raw_items, rates))
    return all_parsed

def get_ebay_access_token():
    client_id, client_secret = os.getenv("EBAY_CLIENT_ID"), os.getenv("EBAY_CLIENT_SECRET")
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(TOKEN_URL, headers={"Authorization": f"Basic {auth}"}, 
                         data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})
    return resp.json()["access_token"]

# ------------------------------
# NOTIFICATIONS
# ------------------------------
def send_email(results, config):
    count = len(results)
    subject = f"[{count} NEW] {config.get('email_subject', 'eBay Results')}"
    def esc(v): return html.escape(str(v))
    html_body = f"<html><body><p>Found {count} items:</p><table border='1'><tr><th>Name</th><th>Price</th><th>Link</th></tr>"
    for r in results:
        html_body += f"<tr><td>{esc(r['Name'])}</td><td>{esc(r['Price'])}</td><td><a href='{r['Link']}'>Link</a></td></tr>"
    html_body += "</table></body></html>"
    msg = MIMEMultipart(); msg["Subject"] = subject; msg["From"] = config["email_sender"]; msg["To"] = config["email_receiver"]
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(config["smtp_server"], int(config.get("smtp_port", 587))) as s:
        s.starttls(); s.login(config["smtp_login"], config["smtp_password"]); s.send_message(msg)

async def send_telegram(results, config):
    """Sends results. The first message includes the 'SEARCH COMPLETE' header."""
    token, chat_id = config.get("telegram_token"), config.get("telegram_chat_id")
    max_run = int(config.get("telegram_max_per_run", 50))
    count_all = len(results)
    
    if len(results) > max_run: results = results[:max_run]

    async with aiohttp.ClientSession() as session:
        for i, r in enumerate(results):
            name = html.escape(r['Name'])
            price = html.escape(r['Price'])
            kw_esc = html.escape(r['Keywords'])
            link = r['Link']
            
            # --- COMBINED HEADER LOGIC ---
            header = ""
            if i == 0:
                now = datetime.now().strftime("%d.%m. at %H:%M")
                header = f"<b>🔎 SEARCH COMPLETE ({now})</b>\nFound <b>{count_all}</b> new items!\n\n"
            
            text = (f"{header}<b>🕹 New find!</b>\n📦 {name}\n💰 {price}\n"
                    f"🔍 <i>Search: {kw_esc}</i>\n🔗 <a href='{link}'>Link to item</a>")
            
            await session.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                               json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False})
            await asyncio.sleep(0.2)

# ------------------------------
# MAIN
# ------------------------------
def main():
    MAX_Q_LEN = 100
    if len(sys.argv) != 2: sys.exit(1)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    with open(sys.argv[1], 'r') as f: config = json.load(f)

    # Build queries
    kw_cfg = config.get("ebay_search_keywords", {})
    req = f"({', '.join(normalize_terms(kw_cfg.get('required_terms', [])))})"
    exclude = normalize_terms(config.get("exclude_terms", []))
    
    queries = []
    for base in normalize_terms(kw_cfg.get("base_terms", [])):
        full_q = f"{req} {base}".strip()
        for exc in exclude:
            if len(f"{full_q} {exc}") <= MAX_Q_LEN: full_q = f"{full_q} {exc}"
            else: break
        queries.append({"keywords": full_q, "base_name": base})

    config["ebay_search_keywords"] = queries
    db_path = os.path.join(script_dir, config.get("ebay_urls_dbfile", "ebay_items.db"))
    create_database_if_not_exists(db_path)
    run_number = db_history_new_run(db_path)
    db_history_set_timestamp(db_path, run_number, "start_time")

    try:
        # FIXED: rates noudetaan Frankfurt-APIsta ja välitetään eteenpäin
        rates = asyncio.run(get_exchange_rates())
        token = get_ebay_access_token()
        all_found = asyncio.run(run_all_searches(config, token, rates))
        
        unique = list({it['itemId']: it for it in all_found if it['itemId']}.values())
        existing = db_search_for_urls(db_path, [it['itemId'] for it in unique])
        new_results = [it for it in unique if it['itemId'] not in existing]

        if new_results:
            db_insert_urls(db_path, [it['itemId'] for it in new_results])
            if config.get("use_email", True): send_email(new_results, config)
            if config.get("use_telegram"):
                asyncio.run(send_telegram(new_results, config))
        logger.info(f"Done. Found {len(new_results)} items.")
    finally:
        db_history_set_timestamp(db_path, run_number, "end_time")

if __name__ == "__main__":
    main()