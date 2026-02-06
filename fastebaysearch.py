#!/usr/bin/python3 -tt
# fastebaysearch 6.2.2026 15:59
# + Enhanced binary search (2/3 fewer searches).
# + Added Telegram support, one item at a time with images.
# + Added extensive error handling.
# + Added exclude_terms
# + Added history table to the DB + log
# + Added currency conversion through Frankfurter API
# - Removed variants search as redundant; required terms can be used.
# Note! Set environment variables via crontab.
# Forked and mostly rewritten from the original ebaysearch v0.4.0 by Kalevi Kolttonen <kalevi@kolttonen.fi>
# (c) 2025-2026 Rakki <rakki@iki.fi>
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
logger.propagate = False  # Prevents duplicate logs in the root logger

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
    """Trim, drop empties/None, coerce to str."""
    return [str(t).strip() for t in (lst or []) if t is not None and str(t).strip()]

def utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def clean_ebay_id(item_id):
    if not item_id:
        return None

    s = str(item_id)

    # 1) Browse API itemId: v1|<digits>|0
    m = re.search(r"\|(\d+)\|", s)
    if m:
        return m.group(1)

    # 2) If number only (legacyItemId)
    m = re.fullmatch(r"\d+", s)
    if m:
        return s

    # 3) Last resort: try to find a longer numeric string
    m = re.search(r"\d{5,}", s)
    return m.group(0) if m else None

def format_date(date_str):
    if not date_str or date_str == "Not available":
        return "Not available"
    try:
        dt = parser.parse(date_str)
        return dt.strftime("%d.%m.%Y %H:%M:%S")
    except Exception as e:
        logger.debug(f"format_date parse failed: {date_str!r}: {e}")
        return "Not available"
        
async def get_exchange_rates():
    """Hakee uusimmat valuuttakurssit Frankfurter API:sta suhteessa euroon."""
    url = "https://api.frankfurter.app/latest?base=EUR&symbols=USD,GBP,AUD"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rates = data.get("rates", {})
                    logger.info(f"Valuuttakurssit päivitetty: {rates}")
                    return rates
                else:
                    logger.warning(f"Frankfurter API virhe {resp.status}. Käytetään alkuperäisiä valuuttoja.")
    except Exception as e:
        logger.error(f"Valuuttakurssien haku epäonnistui: {e}")
    return {}

# ------------------------------
# DATABASE FUNCTIONS
# ------------------------------
def create_database_if_not_exists(db_path):
    """
    Creates:
      - ebayids (as before)
      - history table (like ebaysearch v0.5.2): id, run_number, start_time, end_time
    Also seeds history with (id=0, run_number=0, NULL, NULL) if empty (like v0.5.2 create script).
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ebayids';")
            if not cur.fetchone():
                cur.executescript("""
                    CREATE TABLE ebayids (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_id TEXT UNIQUE NOT NULL,
                        insert_time TEXT NOT NULL
                    );
                    CREATE INDEX idx_item_id ON ebayids (item_id);
                """)
                logger.info(f"✅ Database {db_path} created.")
            # history (v0.5.2 style)
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='history';")
            if not cur.fetchone():
                cur.executescript("""
                    CREATE TABLE history (
                        id INTEGER PRIMARY KEY,
                        run_number INTEGER,
                        start_time TEXT,
                        end_time TEXT
                    );
                """)
                logger.info(f"✅ Table history created in {db_path}")

            # Seed history with run_number=0 row if empty (like v0.5.2)
            cur.execute("SELECT COUNT(*) FROM history;")
            cnt = int(cur.fetchone()[0])
            if cnt == 0:
                cur.execute(
                    "INSERT INTO history (id, run_number, start_time, end_time) VALUES (0, 0, NULL, NULL);"
                )
                logger.info("✅ Seeded history with run_number=0")

            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"❌ Database error: {e}")
        sys.exit(1)

def db_history_new_run(db_path):
    """
    Like v0.5.2:
      SELECT MAX(run_number) FROM history
      INSERT new row with (run_number, NULL, NULL)
    Returns new run_number.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA busy_timeout=30000;")
            cur = conn.cursor()
            cur.execute("SELECT MAX(run_number) FROM history;")
            row = cur.fetchone()
            max_run = int(row[0] or 0)
            new_run = max_run + 1
            cur.execute(
                "INSERT INTO history (run_number, start_time, end_time) VALUES (?, NULL, NULL);",
                (new_run,)
            )
            conn.commit()
            return new_run
    except sqlite3.Error as e:
        logger.error(f"❌ SQLite history new run error: {e}")
        return None

def db_history_set_timestamp(db_path, run_number, timestamp_type):
    """
    Like v0.5.2:
      UPDATE history SET start_time/end_time = <utc_now_str> WHERE run_number = ?
    """
    if run_number is None:
        return
    if timestamp_type not in ("start_time", "end_time"):
        return

    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA busy_timeout=30000;")
            cur = conn.cursor()
            cur.execute(
                f"UPDATE history SET {timestamp_type} = ? WHERE run_number = ?;",
                (utc_now_str(), int(run_number))
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"❌ SQLite history timestamp error: {e}")

def db_search_for_urls(db_path, item_ids, chunk_size=900):
    if not item_ids:
        return set()

    found = set()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA busy_timeout=30000;")
            cur = conn.cursor()
            for i in range(0, len(item_ids), chunk_size):
                chunk = item_ids[i:i+chunk_size]
                q = f"SELECT item_id FROM ebayids WHERE item_id IN ({','.join(['?']*len(chunk))})"
                cur.execute(q, chunk)
                found.update(row[0] for row in cur.fetchall())
        return found
    except sqlite3.Error as e:
        logger.error(f"❌ SQLite search error: {e}")
        return set()

def db_insert_urls(db_path, item_ids):
    if not item_ids:
        return
    insert_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    data = [(iid, insert_time) for iid in item_ids]
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA busy_timeout=30000;")
            cur = conn.cursor()
            cur.executemany("INSERT OR IGNORE INTO ebayids (item_id, insert_time) VALUES (?, ?)", data)
            conn.commit()
            logger.info(f"✅ Added {cur.rowcount} new items to the database.")
    except sqlite3.Error as e:
        logger.error(f"❌ SQLite insertion error: {e}")

# ------------------------------
# CORE LOGIC
# ------------------------------
def parse_search_results(keywords, raw_items, rates):
    results = []
    for item in raw_items:
        raw_id = item.get("itemId") or item.get("legacyItemId")
        item_id = clean_ebay_id(raw_id)
        price_data = item.get("price", {})
        val_str = price_data.get("value", "0.0")
        curr = price_data.get("currency", "EUR").upper()
        
        try:
            val = float(val_str)
        except ValueError:
            val = 0.0

        # Valuuttamuunnos Frankfurterin kertoimilla
        if curr == "EUR":
            display_price = f"{val:.2f} EUR"
        elif curr in rates:
            eur_val = val / rates[curr]
            display_price = f"{eur_val:.2f} EUR ({val:.2f} {curr})"
        else:
            # Jos kurssia ei löydy, näytetään alkuperäinen
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



async def search_ebay(session, token, site, full_query, limit=200):
    all_results = []
    offset = 0
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": site,
        "Accept": "application/json",
    }

    timeout_settings = aiohttp.ClientTimeout(total=30)

    while True:
        params = {"q": full_query, "limit": str(limit), "offset": str(offset)}
        async with session.get("https://api.ebay.com/buy/browse/v1/item_summary/search",
                               headers=headers,
                               params=params,
                               timeout=timeout_settings
                               ) as response:

            if response.status == 429:
                ra = response.headers.get("Retry-After")
                logger.warning(f"429 rate limit ({site}), Retry-After={ra}")
                raise RateLimitError(f"429 rate limit ({site})")


            if response.status != 200:
                err_text = await response.text()
                logger.error(f"API Error ({site}): {response.status} - {err_text}")
                break

            data = await response.json()
            items = data.get("itemSummaries", [])
            all_results.extend(items)

            total = int(data.get("total", 0))
            # Offset >= 1000 limit, as the API doesn't support deeper pagination
            next_offset = offset + limit
            if total <= offset + len(items) or len(items) < limit or next_offset > 1000:
                break
            offset = next_offset
    return all_results

async def run_all_searches(config, token, rates):
    """Executes all searches in parallel and returns a combined list."""
    all_parsed = []
    async with aiohttp.ClientSession() as session:
        tasks = []
        # Luodaan tehtävälista (tupleina: hakusana, korutiini)
        for site in config.get('ebay_sites', []):
            for search_item in config.get('ebay_search_keywords', []):
                # Tallennetaan alkuperäinen base_term metadataan
                base_term = search_item["base_name"]
                kw = search_item["keywords"]
                tasks.append((base_term, search_ebay(session, token, site, kw)))

        logger.info(f"Launching {len(tasks)} parallel API searches...")

        try:
            # return_exceptions=False -> RateLimitError --> immediately break of the first error.
            results_raw = await asyncio.gather(*(t[1] for t in tasks))

        except RateLimitError as e:
            logger.critical(f"⛔ RATE LIMIT hit! Terminating execution. Reason: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in parallel execution: {e}")
            raise

        # All searches done, combine results
        for (base_term, _), raw_items in zip(tasks, results_raw):
            all_parsed.extend(parse_search_results(base_term, raw_items, rates))

    return all_parsed

def get_ebay_access_token():
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)
                if time.time() < data.get("expires_at", 0):
                    return data["access_token"]
    except (OSError, json.JSONDecodeError) as e:
        logger.debug(f"Token cache read failed: {e}")

    client_id = os.getenv("EBAY_CLIENT_ID")
    client_secret = os.getenv("EBAY_CLIENT_SECRET")
    if not client_id or not client_secret:
        logger.error("EBAY_CLIENT_ID or SECRET missing from environment variables!")
        sys.exit(1)

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    try:
        resp = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=20,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Token retrieval failed: {e}")
        sys.exit(1)

    d = resp.json()
    d["expires_at"] = time.time() + d["expires_in"] - 60
    with open(TOKEN_FILE, "w") as f:
        json.dump(d, f)

    return d["access_token"]


def safe_url(url: str) -> str:
    """Allow only http/https-links. Others -> empty."""
    if not url:
        return ""
    u = url.strip()
    p = urlparse(u)
    if p.scheme.lower() in ("http", "https"):
        return u
    return ""


def send_email(results, config):
    """Sends an email using the configuration and the email template."""

    try:
        smtp_server = config["smtp_server"]
        smtp_port = int(config.get("smtp_port", 587))
        smtp_login = config["smtp_login"]
        smtp_password = config["smtp_password"]
        email_sender = config["email_sender"]
        email_receiver = config["email_receiver"]
        email_subject = config["email_subject"]
        email_person_name = config["email_person_name"]
    except KeyError as e:
        logger.error(f"⚠️ Email configuration missing: {e}")
        return

    # HTML escape
    def esc(v):
        return html.escape("" if v is None else str(v))

    def esc_href(v):
        return html.escape("" if v is None else str(v), quote=True)

    sorted_results = sorted(results, key=lambda r: (r.get("Name") or "").lower())

    count = len(results)
    base_subject = config.get("email_subject", "eBay Search Results")
    dynamic_subject = f"[{count} NEW] {base_subject}"

    results_html = f"""
    <html>
    <head>
        <style>
            table {{
                width: 100%;
                border-collapse: collapse;
            }}
            th, td {{
                border: 1px solid #ddd;
                padding: 8px;
                text-align: left;
            }}
            th {{
                background-color: #f2f2f2;
            }}
        </style>
    </head>
    <body>
        <p>Hello {esc(email_person_name)},</p>
        <p>Found <b>{count}</b> new items since last run:</p>
        <table>
            <tr>
                <th>#</th>
                <th>Name</th>
                <th>Ebay Site</th>
                <th>Price</th>
                <th>Seller</th>
                <th>Starts</th>
                <th>Ends</th>
                <th>Keywords</th>
                <th>Link</th>
            </tr>
    """

    for index, r in enumerate(sorted_results, start=1):
        link = safe_url(r.get("Link", ""))
        results_html += f"""
            <tr>
                <td>{index}</td>
                <td>{esc(r.get('Name'))}</td>
                <td>{esc(r.get('Ebay-site'))}</td>
                <td>{esc(r.get('Price'))}</td>
                <td>{esc(r.get('Seller'))}</td>
                <td>{esc(r.get('Starts'))}</td>
                <td>{esc(r.get('Ends'))}</td>
                <td>{esc(r.get('Keywords'))}</td>
                <td><a href="{esc_href(link)}">Link</a></td>
            </tr>
        """

    results_html += """
        </table>
        <p>Best regards,<br>Your eBay Search Bot</p>
    </body>
    </html>
    """

    msg = MIMEMultipart()
    msg["From"] = email_sender
    msg["To"] = email_receiver
    msg["Subject"] = dynamic_subject
    msg.attach(MIMEText(results_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_login, smtp_password)
            server.send_message(msg)
            logger.info(f"📧 Email sent: {dynamic_subject}")
    except smtplib.SMTPAuthenticationError:
        logger.error("❌ SMTP authentication failed – check username and password.")
    except smtplib.SMTPException as e:
        logger.error(f"❌ SMTP error: {e}")


async def send_telegram(results, config):
    """Send each item separately to Telegram with proper timeout and error handling."""
    token = config.get("telegram_token")
    chat_id = config.get("telegram_chat_id")
    max_per_run = int(config.get("telegram_max_per_run", 1000))

    total = len(results)
    if total > max_per_run:
        logger.warning(f"Telegram cap active: {total} -> {max_per_run}")
        results = results[:max_per_run]

    if not token or not chat_id:
        logger.error("❌ Telegram configuration missing!")
        return

    logger.info(f"Sending {len(results)} items to Telegram individually...")

    timeout_settings = aiohttp.ClientTimeout(total=30)
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    async with aiohttp.ClientSession(timeout=timeout_settings) as session:
        for r in results:
            # Raw values
            raw_name = r.get("Name", "Unknown")
            raw_price = r.get("Price", "N/A")
            raw_link = r.get("Link", "")
            raw_keywords = r.get("Keywords", "Unknown")

            # Safety
            name = html.escape(str(raw_name))
            price = html.escape(str(raw_price))
            link = safe_url(raw_link)
            link_esc = html.escape(link, quote=True)
            keyword_esc = html.escape(str(raw_keywords))

            text = (
                "<b>🕹 New find!</b>\n"
                f"📦 {name}\n"
                f"💰 {price}\n"
                f"🔍 <i>Search: {keyword_esc}</i>\n"
                f'🔗 <a href="{link_esc}">Link to item</a>'
            )

            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False
            }

            try:
                async with session.post(url, json=payload) as resp:
                    data = await resp.json(content_type=None)

                    if resp.status != 200 or not data.get("ok", False):
                        logger.error(
                            f"❌ Telegram error for {raw_name}: "
                            f"HTTP {resp.status} - {data}"
                        )

            except asyncio.TimeoutError:
                logger.error(f"⏱ Telegram timeout for {raw_name}")
            except aiohttp.ClientError as e:
                logger.error(f"❌ Telegram network error for {raw_name}: {e}")
            except Exception as e:
                logger.error(f"❌ Telegram unexpected error for {raw_name}: {e}")

            # Flood protection (Telegram ~30 msg/s max)
            await asyncio.sleep(0.2)

    logger.info("✅ All Telegram notifications sent.")


async def send_telegram_header(count, config):
    """Add a header to the Telegram channel before sending actual links and thumbnails."""
    token = config.get("telegram_token")
    chat_id = config.get("telegram_chat_id")
    if not token or not chat_id:
        return

    now = datetime.now().strftime("%d.%m. at %H:%M")
    text = f"<b>🔎 SEARCH COMPLETE ({now})</b>\nFound <b>{count}</b> new items!"

    async with aiohttp.ClientSession() as session:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        await session.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)

# ------------------------------
# MAIN
# ------------------------------
def main():
    MAX_Q_LEN = 100

    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} config.json")
        sys.exit(1)

    # Absolute path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    with open(sys.argv[1], 'r') as f:
        config = json.load(f)

    kw_cfg = config.get("ebay_search_keywords", {})
    queries = []

    req_list = normalize_terms(kw_cfg.get("required_terms", []))
    required_clause = f"({', '.join(req_list)})" if req_list else ""

    exclude_list = normalize_terms(config.get("exclude_terms", []))

    logger.info("Generating comprehensive search...")

    for base in normalize_terms(kw_cfg.get("base_terms", [])):
            full_query = f"{required_clause} {base}".strip()

            # Avoid over 100 characters exclude (eBay API limitation)
            for exc in exclude_list:
                if len(f"{full_query} {exc}") <= MAX_Q_LEN:
                    full_query = f"{full_query} {exc}"
                else:
                    break

            queries.append({
                "keywords": full_query,
                "base_name": base
            })

    config["ebay_search_keywords"] = queries
    logger.info(f"Generated {len(queries)} optimized searches.")
    if queries:
        logger.info(f"Example: {queries[0]['keywords']}")
    else:
        logger.error("No searches generated (base_terms empty?)")
        sys.exit(1)


    # Secure the absolute DB path
    db_name = config.get("ebay_urls_dbfile", "ebay_items.db")
    db_path = os.path.join(script_dir, db_name)

    # Ensure schema incl. history
    create_database_if_not_exists(db_path)

    # History run tracking
    run_number = db_history_new_run(db_path)
    logger.info(f"🧾 History run started: run_number={run_number}, db={os.path.abspath(db_path)}")

    db_history_set_timestamp(db_path, run_number, "start_time")
    logger.info(f"🧾 History start_time set for run_number={run_number}")


    token = get_ebay_access_token()

    try:
        try:
            rates = asyncio.run(get_exchange_rates())
            all_found_items = asyncio.run(run_all_searches(config, token, rates))
        except RateLimitError as e:
            logger.critical(f"⛔ RATE LIMIT – exit(2): {e}")
            # stamp end_time before exiting
            logger.info(f"🧾 History end_time set for run_number={run_number}")
            sys.exit(2)

        unique_items = list({it['itemId']: it for it in all_found_items}.values())

        logger.info(f"All items found (raw): {len(all_found_items)}")
        logger.info(f"Unique item IDs: {len(unique_items)}")
        logger.info(f"DB absolute path: {os.path.abspath(db_path)}")

        item_ids = [it['itemId'] for it in unique_items]
        existing_ids = db_search_for_urls(db_path, item_ids)

        logger.info(f"IDs queried from DB: {len(item_ids)}")
        logger.info(f"Existing in DB: {len(existing_ids)}")

        new_results = [it for it in unique_items if it['itemId'] not in existing_ids]
        logger.info(f"New results found: {len(new_results)}")

        if new_results:
            db_insert_urls(db_path, [it['itemId'] for it in new_results])

            if config.get("use_email", True):
                send_email(new_results, config)

            if config.get("use_telegram"):
                async def notify_telegram():
                    await send_telegram_header(len(new_results), config)
                    await send_telegram(new_results, config)
                asyncio.run(notify_telegram())
        else:
            logger.info("No new items found.")

        logger.info(
            f"Done. Searched {len(all_found_items)} items, {len(new_results)} new. "
            f"Duration: {time.time()-start_time:.2f}s"
        )
    finally:
        if run_number is not None:
            db_history_set_timestamp(db_path, run_number, "end_time")

if __name__ == "__main__":
    main()
