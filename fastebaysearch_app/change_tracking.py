from dataclasses import asdict, replace
from decimal import Decimal
from datetime import datetime, timezone
import json

from .models import SearchResult
from .utils import utc_now_str


def migrate(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS item_observations (item_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS change_events (id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, created_at TEXT NOT NULL, old_snapshot TEXT NOT NULL, snapshot TEXT NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS change_deliveries (event_id INTEGER NOT NULL, channel TEXT NOT NULL, delivered_at TEXT, attempted_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, PRIMARY KEY(event_id, channel))")


def observe(cur, results, new_ids, html_enabled):
    for item in results:
        row = cur.execute("SELECT snapshot FROM item_observations WHERE item_id=?", (item.item_id,)).fetchone()
        old = json.loads(row[0]) if row else None
        changes = []
        if old:
            old_options = set(old['buying_options']) & {'AUCTION', 'FIXED_PRICE'}
            options = set(item.buying_options) & {'AUCTION', 'FIXED_PRICE'}
            if old_options and options and old_options != options:
                changes.append('Buying options: ' + ', '.join(sorted(old_options)) + ' -> ' + ', '.join(sorted(options)))
            if (old['fixed_amount'] and item.fixed_amount and item.fixed_currency
                    and old['fixed_currency'] == item.fixed_currency
                    and old['source_site'] == item.source_site
                    and 'FIXED_PRICE' in old_options and 'FIXED_PRICE' in options
                    and Decimal(item.fixed_amount) < Decimal(old['fixed_amount'])):
                changes.append(f"Price decreased: {old['fixed_amount']} -> {item.fixed_amount} {item.fixed_currency}")
        snapshot = json.dumps(asdict(item))
        cur.execute("INSERT INTO item_observations VALUES (?,?) ON CONFLICT(item_id) DO UPDATE SET snapshot=excluded.snapshot", (item.item_id, snapshot))
        cur.execute("UPDATE ebayids SET keywords=?, title=?, url=?, site=?, price=?, seller=?, starts=?, ends=?, image=? WHERE item_id=?",
                    (item.keywords,item.name,item.link,item.ebay_site,item.price,item.seller,item.starts,item.ends,item.image,item.item_id))
        if changes and item.item_id not in new_ids:
            event = replace(item, change_description='; '.join(changes))
            cur.execute("INSERT INTO change_events(item_id,created_at,old_snapshot,snapshot) VALUES (?,?,?,?)", (item.item_id,datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),row[0],json.dumps(asdict(event))))
            event_id = cur.lastrowid
            flags = cur.execute("SELECT email_notified_at,telegram_notified_at FROM ebayids WHERE item_id=?",(item.item_id,)).fetchone()
            for channel, delivered in zip(('email','telegram'), flags):
                if delivered:
                    cur.execute("INSERT INTO change_deliveries(event_id,channel) VALUES (?,?)",(event_id,channel))
            html = cur.execute("SELECT delivered_at FROM html_notifications WHERE item_id=?",(item.item_id,)).fetchone()
            if html_enabled and (html is None or html[0]):
                cur.execute("INSERT INTO change_deliveries(event_id,channel) VALUES (?, 'html')",(event_id,))


def pending(conn, channel, originals, limit, claim=False, retry_after_hours=6, max_attempts=3):
    candidates = []
    for item in originals:
        row = conn.execute("SELECT insert_time FROM ebayids WHERE item_id=?",(item.item_id,)).fetchone()
        candidates.append((row[0], 0, item))
    rows = conn.execute("""SELECT e.id,e.created_at,e.snapshot FROM change_events e JOIN change_deliveries d ON e.id=d.event_id
        WHERE d.channel=? AND d.delivered_at IS NULL
        AND (d.channel != 'telegram' OR (d.attempts < ? AND (d.attempted_at IS NULL OR d.attempted_at <= datetime('now',?))))
        ORDER BY e.created_at,e.id""",(channel, max_attempts, f"-{retry_after_hours} hours")).fetchall()
    for event_id, created, snapshot in rows:
        data = json.loads(snapshot)
        data['buying_options'] = tuple(data['buying_options'])
        candidates.append((created, event_id, replace(SearchResult(**data),event_id=event_id)))
    selected = [c[2] for c in sorted(candidates,key=lambda c:(c[0],c[1]))][:limit]
    if channel == 'telegram' and claim:
        chosen = {i.item_id for i in selected if not i.event_id}
        for item in originals:
            if item.item_id not in chosen:
                conn.execute("UPDATE ebayids SET telegram_attempted_at=NULL,telegram_attempt_count=MAX(0,telegram_attempt_count-1) WHERE item_id=?",(item.item_id,))
        for item in selected:
            if item.event_id:
                conn.execute("UPDATE change_deliveries SET attempted_at=?,attempts=attempts+1 WHERE event_id=? AND channel=?",(utc_now_str(),item.event_id,channel))
    return selected


def mark(conn, channel, items, timestamp, error, release=False):
    for item in items:
        if item.event_id:
            conn.execute("UPDATE change_deliveries SET delivered_at=?,error=? WHERE event_id=? AND channel=?",(timestamp,error,item.event_id,channel))
            if release:
                conn.execute("UPDATE change_deliveries SET attempted_at=NULL,attempts=MAX(0,attempts-1) WHERE event_id=? AND channel=?",(item.event_id,channel))
