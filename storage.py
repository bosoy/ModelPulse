#!/usr/bin/env python3
"""ModelPulse storage: SQLite history of probe results and scan runs."""

import os
import sqlite3
import threading
import time

import core

DB_PATH = os.path.join(core.APP_DIR, "data", "modelpulse.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    finished_at REAL,
    total       INTEGER DEFAULT 0,
    replied     INTEGER DEFAULT 0,
    empty       INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    source      TEXT,
    trigger     TEXT
);
CREATE TABLE IF NOT EXISTS probes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    ts          REAL NOT NULL,
    model       TEXT NOT NULL,
    host        TEXT,
    provider_id TEXT,
    status      TEXT NOT NULL,
    http_code   INTEGER,
    ms          INTEGER,
    reply       TEXT,
    error       TEXT,
    wait_s      INTEGER DEFAULT 0,
    wait_kind   TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_probes_model_ts ON probes(model, ts);
CREATE INDEX IF NOT EXISTS idx_probes_run ON probes(run_id);
CREATE TABLE IF NOT EXISTS hidden (
    model       TEXT NOT NULL,
    host        TEXT NOT NULL,
    provider_id TEXT,
    hidden_at   REAL NOT NULL,
    reason_code TEXT,
    reason_text TEXT,
    PRIMARY KEY (model, host)
);
"""


class Store(object):
    def __init__(self, path=DB_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self):
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(probes)")}
        for col, decl in (("wait_s", "INTEGER DEFAULT 0"), ("wait_kind", "TEXT")):
            if col not in have:
                self._conn.execute("ALTER TABLE probes ADD COLUMN %s %s" % (col, decl))

    # -- writes ---------------------------------------------------------
    def start_run(self, source, trigger, started_at=None):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (started_at, source, trigger) VALUES (?,?,?)",
                (started_at or time.time(), source, trigger))
            self._conn.commit()
            return cur.lastrowid

    def finish_run(self, run_id, summary):
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished_at=?, total=?, replied=?, empty=?, failed=? "
                "WHERE id=?",
                (time.time(), summary["total"], summary["replied"],
                 summary["empty"], summary["failed"], run_id))
            self._conn.commit()

    def add_probes(self, run_id, results):
        rows = [(run_id, time.time(), r["model"], r["host"], r["provider_id"],
                 r["status"], r["http_code"], r["ms"], r["reply"], r["error"],
                 r.get("wait_s") or 0, r.get("wait_kind") or "")
                for r in results]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO probes (run_id, ts, model, host, provider_id, status,"
                " http_code, ms, reply, error, wait_s, wait_kind)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self._conn.commit()

    # -- hidden (muted) models -------------------------------------------
    def hide(self, model, host, reason_code="", reason_text="", provider_id=""):
        with self._lock:
            self._conn.execute(
                "INSERT INTO hidden (model, host, provider_id, hidden_at,"
                " reason_code, reason_text) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(model, host) DO UPDATE SET hidden_at=excluded.hidden_at,"
                " reason_code=excluded.reason_code, reason_text=excluded.reason_text",
                (model, host, provider_id, time.time(), reason_code, reason_text))
            self._conn.commit()

    def unhide(self, model, host):
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM hidden WHERE model=? AND host=?", (model, host))
            self._conn.commit()
            return cur.rowcount > 0

    def hidden_set(self):
        """{(model, host)} currently muted, for skipping them during a scan."""
        with self._lock:
            rows = self._conn.execute("SELECT model, host FROM hidden").fetchall()
        return {(r["model"], r["host"]) for r in rows}

    def hidden_list(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM hidden ORDER BY hidden_at DESC").fetchall()
        return [dict(r) for r in rows]

    # -- reads ----------------------------------------------------------
    def last_run(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def latest_per_model(self, min_runs=3):
        """Most recent probe row for every (model, host) pair, plus a dead
        verdict and its muted flag. History is kept regardless of hiding."""
        sql = """
        SELECT p.* FROM probes p
        JOIN (SELECT model, host, MAX(ts) AS mts FROM probes GROUP BY model, host) x
          ON p.model = x.model AND p.host = x.host AND p.ts = x.mts
        ORDER BY p.host, p.model
        """
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        hidden = self.hidden_set()
        out = []
        for r in rows:
            d = dict(r)
            d["key"] = (r["host"] or "") + "|" + r["model"]
            d["history"] = self._history(r["model"], r["host"], 40)
            is_dead, code, text = core.classify_dead(d["history"], min_runs=min_runs)
            d["dead"] = is_dead
            d["reason_code"] = code or ""
            d["reason_text"] = text
            d["hidden"] = (r["model"], r["host"]) in hidden
            out.append(d)
        return out

    def _history(self, model, host, limit=40):
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, status, ms, http_code, error, wait_s, wait_kind FROM probes "
                "WHERE model=? AND host=? ORDER BY ts DESC LIMIT ?",
                (model, host, limit)).fetchall()
        return [{"ts": r["ts"], "status": r["status"], "ms": r["ms"],
                 "http_code": r["http_code"], "error": r["error"],
                 "wait_s": r["wait_s"] or 0, "wait_kind": r["wait_kind"] or ""}
                for r in reversed(rows)]

    def model_detail(self, model, host):
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, status, ms, http_code, reply, error, wait_s, wait_kind "
                "FROM probes WHERE model=? AND host=? ORDER BY ts DESC LIMIT 200",
                (model, host)).fetchall()
        return [dict(r) for r in rows]

    def recent_runs(self, limit=50):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def uptime(self, model, host, since_ts):
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) c FROM probes WHERE model=? AND host=? "
                "AND ts>=? GROUP BY status", (model, host, since_ts)).fetchall()
        counts = {r["status"]: r["c"] for r in rows}
        total = sum(counts.values())
        if not total:
            return None
        return {
            "total": total,
            "reply": counts.get("reply", 0),
            "empty": counts.get("empty", 0),
            "fail": counts.get("fail", 0),
            "reply_pct": round(100.0 * counts.get("reply", 0) / total, 1),
        }

    def known_models(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT model, host, provider_id FROM probes "
                "ORDER BY host, model").fetchall()
        return [dict(r) for r in rows]
