#!/usr/bin/env python3
"""ModelPulse server: background scheduler + HTTP API + dashboard host.

Binds to 127.0.0.1 only. API keys never leave this process.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core
import storage

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
STATE = {
    "scanning": False,
    "last_scan_started": None,
    "last_scan_finished": None,
    "interval": 900,
    "max_tokens": 16,
    "timeout": 30.0,
    "source_path": None,
    "source_mtime": None,
    "source_agent": "Qoder",
    "active_source": "qoder",
    "deep_scan": False,
    "provider_count": 0,
    "model_count": 0,
    "last_error": None,
    "language": "ru",
    "settings_override": "",
}
STATE_LOCK = threading.Lock()
STORE = None
RUN_LOCK = threading.Lock()


def do_scan(trigger):
    """Run one full scan and persist it. Serialized so scans never overlap."""
    if not RUN_LOCK.acquire(blocking=False):
        return None, "scan already running"
    try:
        with STATE_LOCK:
            STATE["scanning"] = True
            STATE["last_scan_started"] = time.time()
            active = STATE["active_source"]
            deep = STATE["deep_scan"]
            override = (STATE.get("settings_override") or "").strip()
        if override:
            # explicit path wins: keep the classic single-file behaviour
            src = None
            path, mtime, targets = core.load_targets(override)
        else:
            src, path, mtime, targets = core.load_targets_for(active, deep=deep)
        with STATE_LOCK:
            STATE["source_path"] = path
            STATE["source_mtime"] = mtime
            STATE["source_agent"] = (src["agent"] if src else "Qoder")
            STATE["provider_count"] = len({t["provider_id"] for t in targets})
            STATE["model_count"] = len(targets)
            STATE["last_error"] = None
        run_id = STORE.start_run(path, trigger)
        results = core.scan(targets, timeout=STATE["timeout"],
                            max_tokens=STATE["max_tokens"])
        STORE.add_probes(run_id, results)
        summary = core.summarize(results)
        STORE.finish_run(run_id, summary)
        with STATE_LOCK:
            STATE["last_scan_finished"] = time.time()
            STATE["scanning"] = False
        return run_id, None
    except Exception as e:
        with STATE_LOCK:
            STATE["scanning"] = False
            STATE["last_error"] = str(e)
        return None, str(e)
    finally:
        RUN_LOCK.release()


def scheduler_loop():
    while True:
        with STATE_LOCK:
            interval = STATE["interval"]
        time.sleep(interval)
        do_scan("schedule")


def open_in_explorer(path):
    try:
        if not os.path.isdir(path):
            return False, "not a directory: " + str(path)
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True, ""
    except Exception as e:
        return False, str(e)


def summary_payload():
    last = STORE.last_run()
    with STATE_LOCK:
        st = dict(STATE)
    return {"state": st, "last_run": last}


class Handler(BaseHTTPRequestHandler):
    server_version = "ModelPulse/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, relpath, ctype):
        full = os.path.join(UI_DIR, relpath)
        if not os.path.abspath(full).startswith(os.path.abspath(UI_DIR)):
            self._json({"error": "forbidden"}, 403)
            return
        if not os.path.isfile(full):
            self._json({"error": "not found"}, 404)
            return
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # never let the browser keep a stale dashboard after we edit it
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p in ("/", "/index.html"):
            self._file("index.html", "text/html; charset=utf-8")
        elif p == "/app.js":
            self._file("app.js", "application/javascript; charset=utf-8")
        elif p == "/style.css":
            self._file("style.css", "text/css; charset=utf-8")
        elif p == "/favicon.png":
            self._file("favicon.png", "image/png")
        elif p == "/favicon.ico":
            self._file("favicon.png", "image/png")
        elif p == "/i18n.ru.json":
            self._file("i18n.ru.json", "application/json; charset=utf-8")
        elif p == "/api/config":
            cfg = core.load_config()
            with STATE_LOCK:
                st = dict(STATE)
            self._json({
                "config": cfg,
                "effective_path": st.get("source_path") or core.resolve_settings_path(),
                "override": st.get("settings_override") or "",
                "app_dir": core.APP_DIR,
                "config_path": core.CONFIG_PATH,
                "default_path": core.DEFAULT_SETTINGS,
            })
        elif p == "/api/source":
            with STATE_LOCK:
                st = dict(STATE)
            self._json({
                "path": st["source_path"] or core.settings_path(),
                "mtime": st["source_mtime"],
                "agent": st["source_agent"],
                "active": st["active_source"],
                "deep": st["deep_scan"],
                "providers": st["provider_count"],
                "models": st["model_count"],
                "interval": st["interval"],
                "scanning": st["scanning"],
                "last_error": st["last_error"],
            })
        elif p == "/api/sources":
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            deep = (q.get("deep") or ["0"])[0] in ("1", "true", "yes")
            with STATE_LOCK:
                active = STATE["active_source"]
            try:
                sources = core.list_sources(deep=deep)
            except Exception as e:
                self._json({"error": str(e)}, 500)
                return
            for s in sources:
                s["active"] = (s["id"] == active)
            self._json({"sources": sources, "active": active})
        elif p == "/api/summary":
            last = STORE.last_run()
            with STATE_LOCK:
                st = dict(STATE)
            self._json({"state": st, "last_run": last,
                        "recent_runs": STORE.recent_runs(30)})
        elif p == "/api/models":
            rows = STORE.latest_per_model()
            now = time.time()
            for r in rows:
                r["uptime_24h"] = STORE.uptime(r["model"], r["host"], now - 86400)
                r["uptime_1h"] = STORE.uptime(r["model"], r["host"], now - 3600)
            self._json({"models": rows})
        elif p.startswith("/api/model/"):
            from urllib.parse import unquote, parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            model = unquote(p[len("/api/model/"):])
            host = (q.get("host") or [""])[0]
            self._json({"model": model, "host": host,
                        "probes": STORE.model_detail(model, host),
                        "uptime_24h": STORE.uptime(model, host, time.time() - 86400)})
        elif p == "/api/runs":
            self._json({"runs": STORE.recent_runs(200)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = self.path.split("?", 1)[0]
        if p == "/api/run":
            if STATE["scanning"]:
                self._json({"ok": False, "reason": "scanning"}, 409)
                return
            threading.Thread(target=do_scan, args=("manual",), daemon=True).start()
            self._json({"ok": True, "started": True})
        elif p == "/api/settings":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            with STATE_LOCK:
                if isinstance(data.get("interval"), (int, float)):
                    STATE["interval"] = max(60, int(data["interval"]))
                if isinstance(data.get("max_tokens"), (int, float)):
                    STATE["max_tokens"] = max(1, min(4096, int(data["max_tokens"])))
                if isinstance(data.get("timeout"), (int, float)):
                    STATE["timeout"] = max(5.0, min(120.0, float(data["timeout"])))
                if "settings_path" in data:
                    STATE["settings_override"] = str(data.get("settings_path") or "").strip()
                st = dict(STATE)
            try:
                core.save_config({
                    "interval_seconds": st["interval"],
                    "max_tokens": st["max_tokens"],
                    "timeout_seconds": st["timeout"],
                    "settings_path": st["settings_override"],
                })
            except Exception:
                pass
            self._json({"ok": True, "state": st})
        elif p == "/api/source":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            with STATE_LOCK:
                if "active" in data:
                    STATE["active_source"] = str(data.get("active") or "qoder").strip()
                if "deep" in data:
                    STATE["deep_scan"] = bool(data.get("deep"))
                st = dict(STATE)
            try:
                core.save_config({"active_source": st["active_source"]})
            except Exception:
                pass
            self._json({"ok": True, "active": st["active_source"],
                        "deep": st["deep_scan"]})
        elif p == "/api/open-folder":
            target = core.APP_DIR
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                which = (payload.get("which") or "app").lower()
                if which == "report":
                    target = os.path.join(core.APP_DIR, "data")
                elif which == "settings":
                    sp = core.resolve_settings_path()
                    target = os.path.dirname(sp) or core.APP_DIR
            except Exception:
                pass
            ok, err = open_in_explorer(target)
            self._json({"ok": ok, "path": target, "error": err})
        else:
            self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--interval", type=int, default=900)
    ap.add_argument("--no-scan-on-start", action="store_true")
    args = ap.parse_args()

    global STORE
    STORE = storage.Store()
    cfg = core.load_config()
    with STATE_LOCK:
        STATE["interval"] = max(60, int(cfg.get("interval_seconds") or args.interval))
        STATE["max_tokens"] = max(1, int(cfg.get("max_tokens") or 16))
        STATE["timeout"] = max(5.0, float(cfg.get("timeout_seconds") or 30))
        STATE["language"] = cfg.get("language") or "ru"
        STATE["settings_override"] = (cfg.get("settings_path") or "").strip()
        STATE["active_source"] = (cfg.get("active_source") or "qoder").strip()

    threading.Thread(target=scheduler_loop, daemon=True).start()

    if not args.no_scan_on_start:
        def boot():
            time.sleep(1.0)
            do_scan("startup")
        threading.Thread(target=boot, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("ModelPulse on http://%s:%d  (interval %ds)" % (args.host, args.port,
                                                         STATE["interval"]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
