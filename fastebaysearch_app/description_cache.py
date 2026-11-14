"""Persistent full descriptions and confident decisions; never caches transport failures."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass

from .recommendation_filter import Decision

CHECK_VERSION = 1


def migrate(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS description_cache (
        app_key TEXT NOT NULL, site TEXT NOT NULL, rest_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, fetched_at REAL NOT NULL, description TEXT NOT NULL,
        country TEXT NOT NULL, PRIMARY KEY(app_key,site,rest_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS validation_cache (
        app_key TEXT NOT NULL, site TEXT NOT NULL, rest_id TEXT NOT NULL,
        rule_key TEXT NOT NULL, fingerprint TEXT NOT NULL, fetched_at REAL NOT NULL,
        status TEXT NOT NULL, reason TEXT NOT NULL, missing_groups TEXT NOT NULL,
        PRIMARY KEY(app_key,site,rest_id,rule_key))""")


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def summary_fingerprint(item):
    # Price, bids and quantity remain fresh search data; they do not invalidate a description.
    fields = ("title", "subtitle", "itemCreationDate", "itemEndDate", "condition", "conditionId",
              "sellerItemRevision", "itemRevision", "itemLastUpdatedDate")
    value = {key: item.get(key) for key in fields}
    categories = item.get("categories")
    value["categories"] = sorted(str(c.get("categoryId")) for c in (categories if isinstance(categories, list) else [])
                                 if isinstance(c, dict))
    seller = item.get("seller")
    value["seller"] = seller.get("username") if isinstance(seller, dict) else None
    return digest(value)


def validation_key(groups, validate_terms, filter_recommendations):
    return digest({"version": CHECK_VERSION, "groups": groups, "validate_terms": validate_terms,
                   "filter_recommendations": filter_recommendations})


@dataclass(frozen=True)
class DescriptionEntry:
    fingerprint: str
    fetched_at: float
    description: str
    country: str = ""


class DescriptionCache:
    def __init__(self, connect, client_id, ttl_hours=24, logger=None):
        self.connect = connect
        self.app_key = hashlib.sha256(("production:" + client_id).encode()).hexdigest()
        self.ttl_hours = ttl_hours
        self.logger = logger or logging.getLogger("fastebaysearch")

    def _fresh(self, stamp, now):
        return self.ttl_hours > 0 and 0 <= now - stamp < self.ttl_hours * 3600

    def get_description(self, site, rest_id, fingerprint, now):
        try:
            with self.connect() as conn:
                row = conn.execute("SELECT fingerprint,fetched_at,description,country FROM description_cache "
                                   "WHERE app_key=? AND site=? AND rest_id=?", (self.app_key, site, rest_id)).fetchone()
            if (row and row[0] == fingerprint and self._fresh(row[1], now)
                    and isinstance(row[2], str) and row[2].strip()):
                return DescriptionEntry(*row)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self.logger.warning("Description cache read failed: %s", type(exc).__name__)
        return None

    def get_decision(self, site, rest_id, fingerprint, rule_key, now):
        try:
            with self.connect() as conn:
                row = conn.execute("""SELECT v.status,v.reason,v.missing_groups,d.country,d.fetched_at
                    FROM validation_cache v JOIN description_cache d
                    ON v.app_key=d.app_key AND v.site=d.site AND v.rest_id=d.rest_id
                    AND v.fingerprint=d.fingerprint AND v.fetched_at=d.fetched_at
                    WHERE v.app_key=? AND v.site=? AND v.rest_id=? AND v.rule_key=? AND v.fingerprint=?""",
                                   (self.app_key, site, rest_id, rule_key, fingerprint)).fetchone()
            if row and self._fresh(row[4], now) and row[0] in ("accepted", "rejected"):
                groups = json.loads(row[2])
                if isinstance(groups, list) and all(isinstance(g, list) and all(isinstance(t, str) for t in g) for g in groups):
                    return Decision(row[0], row[1], tuple(tuple(g) for g in groups)), row[3]
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self.logger.warning("Validation cache read failed: %s", type(exc).__name__)
        return None

    def put(self, site, rest_id, entry, rule_key, decision):
        if decision.status not in ("accepted", "rejected") or self.ttl_hours <= 0:
            return False
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                old = conn.execute("SELECT fingerprint,fetched_at FROM description_cache WHERE app_key=? AND site=? AND rest_id=?",
                                   (self.app_key, site, rest_id)).fetchone()
                if old and old[1] > entry.fetched_at:
                    return False  # A concurrent run has already fetched a newer version.
                if not old or tuple(old) != (entry.fingerprint, entry.fetched_at):
                    conn.execute("DELETE FROM validation_cache WHERE app_key=? AND site=? AND rest_id=?",
                                 (self.app_key, site, rest_id))
                    conn.execute("INSERT OR REPLACE INTO description_cache VALUES (?,?,?,?,?,?,?)",
                                 (self.app_key, site, rest_id, entry.fingerprint, entry.fetched_at, entry.description, entry.country))
                conn.execute("INSERT OR REPLACE INTO validation_cache VALUES (?,?,?,?,?,?,?,?,?)",
                             (self.app_key, site, rest_id, rule_key, entry.fingerprint, entry.fetched_at,
                              decision.status, decision.reason, json.dumps(decision.missing_groups)))
            return True
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self.logger.warning("Description cache write failed: %s", type(exc).__name__)
            return False

    def prune(self, now):
        try:
            with self.connect() as conn:
                conn.execute("DELETE FROM validation_cache WHERE app_key=? AND fetched_at<=?",
                             (self.app_key, now - self.ttl_hours * 3600))
                conn.execute("DELETE FROM description_cache WHERE app_key=? AND fetched_at<=?",
                             (self.app_key, now - self.ttl_hours * 3600))
        except sqlite3.Error as exc:
            self.logger.warning("Description cache cleanup failed: %s", type(exc).__name__)
