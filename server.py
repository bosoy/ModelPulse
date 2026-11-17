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
import locate
import report
import storage

UI_DIR = os.path.join(core.RES_DIR, "ui")
STATE = {
    "scanning": False,
    "last_scan_started": None,
    "last_scan_finished": None,
    "interval": 900,
    "max_tokens": core.DEFAULT_MAX_TOKENS,
    "timeout": 30.0,
    "source_path": None,
    "source_mtime": None,
    "source_agent": "Qoder",
    "active_source": "qoder",
    "deep_scan": False,
    "provider_count": 0,
    "model_count": 0,
    "last_error": None,
    "last_cancelled": False,
    "language": "ru",
    "theme": "system",
    "dead_min_runs": 3,
    "settings_override": "",
    "scan_total": 0,
    "scan_done": 0,
    "scan_current": None,
    "scan_inflight": [],
    "auto": False,
    "scan_mode": "full",
}
STATE_LOCK = threading.Lock()
STORE = None
RUN_LOCK = threading.Lock()
CANCEL = threading.Event()
# auto (continuous) scanning: AUTO holds the on/off state, AUTO_WAKE nudges the
# scheduler to re-read it at once instead of sleeping out the whole interval
AUTO = threading.Event()
AUTO_WAKE = threading.Event()


def do_scan(trigger, mode_override=None):
    """Run one full scan and persist it. Serialized so scans never overlap.

    A cancelled pass is dropped entirely: no run row and no probe rows are
    written, so partial results never reach history or uptime statistics.
    mode_override forces 'quick' or 'full' for this pass only.
    """
    if not RUN_LOCK.acquire(blocking=False):
        return None, "scan already running"
    CANCEL.clear()
    try:
        with STATE_LOCK:
            STATE["scanning"] = True
            STATE["last_cancelled"] = False
            started_at = time.time()
            STATE["last_scan_started"] = started_at
            active = STATE["active_source"]
            deep = STATE["deep_scan"]
            override = (STATE.get("settings_override") or "").strip()
            scan_mode = mode_override if mode_override in core.SCAN_MODES \
                else (STATE.get("scan_mode") or "full")
            scan_lang = STATE.get("language") or "ru"
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
            STATE["last_error"] = None
        # muted models are not probed at all: their history stays, but we save
        # time and quota on ones the user already wrote off
        try:
            muted = STORE.hidden_set()
        except Exception:
            muted = set()
        if muted:
            targets = [t for t in targets
                       if (t["model"], t["host"]) not in muted]
        # probe the models with the best track record first: a scan the user
        # cancels early then still contains the answers they care about most
        try:
            scores = STORE.score_map()
        except Exception:
            scores = {}

        def _rank(t):
            # highest reply-share first; unknown models sit in the middle so a
            # brand-new pair is neither buried nor jumped ahead of a proven one
            s = scores.get((t["model"], t["host"]))
            return -(s if s is not None else 0.5)

        with STATE_LOCK:
            STATE["model_count"] = len(targets)
            STATE["scan_total"] = len(targets)
            STATE["scan_done"] = 0
            STATE["scan_current"] = None
            STATE["scan_inflight"] = []

        def on_start(t):
            # fired inside a worker thread the instant a probe really starts its
            # request, so the UI can name models that are in flight right now -
            # not the last one that finished (which made a slow pass look hung)
            with STATE_LOCK:
                STATE["scan_inflight"].append(
                    {"model": t.get("model"), "host": t.get("host")})

        def on_result(r):
            # streamed from core.scan as each probe lands, so the UI can show
            # real progress: how many checked, which model, and its timing
            with STATE_LOCK:
                STATE["scan_done"] += 1
                STATE["scan_current"] = {
                    "model": r.get("model"), "host": r.get("host"),
                    "status": r.get("status"), "ms": r.get("ms"),
                }
                # drop this one from the in-flight list (host+model is unique
                # per probe, so we can match it back without an id)
                live = STATE["scan_inflight"]
                for i, x in enumerate(live):
                    if x.get("model") == r.get("model") \
                            and x.get("host") == r.get("host"):
                        live.pop(i)
                        break

        results = core.scan(targets, timeout=STATE["timeout"],
                            max_tokens=STATE["max_tokens"],
                            should_stop=CANCEL.is_set, on_result=on_result,
                            on_start=on_start, mode=scan_mode, sort_key=_rank,
                            lang=scan_lang)
        cancelled = CANCEL.is_set()
        summary = core.summarize(results)
        run_id = None
        # a cancelled pass is no longer thrown away: whatever finished before the
        # stop is saved and shown, flagged partial so history is not read as a
        # clean full sweep. An empty cancel (nothing landed) still writes nothing.
        if results:
            run_id = STORE.start_run(path, trigger, started_at=started_at)
            STORE.add_probes(run_id, results)
            STORE.finish_run(run_id, summary, partial=cancelled)
        with STATE_LOCK:
            STATE["last_scan_finished"] = time.time()
            STATE["scanning"] = False
            STATE["scan_inflight"] = []
            STATE["last_cancelled"] = cancelled
            if cancelled:
                STATE["last_error"] = None
        return run_id, None
    except Exception as e:
        with STATE_LOCK:
            STATE["scanning"] = False
            STATE["scan_inflight"] = []
            STATE["last_error"] = str(e)
        return None, str(e)
    finally:
        CANCEL.clear()
        RUN_LOCK.release()


def scheduler_loop():
    """Continuous scanning, driven by the AUTO toggle.

    Parked while AUTO is off, so nothing runs unless the user asks for it or
    turns continuity on. On each turn: scan, then wait out the interval (an
    AUTO_WAKE from the toggle cuts the wait short immediately).
    """
    while True:
        if not AUTO.is_set():
            AUTO_WAKE.wait()
            AUTO_WAKE.clear()
            continue
        do_scan("schedule")
        with STATE_LOCK:
            interval = STATE["interval"]
        AUTO_WAKE.wait(timeout=interval)
        AUTO_WAKE.clear()


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

    # a windowed exe has no stderr, so the inherited writer would crash
    def log_error(self, fmt, *args):
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
        elif p.startswith("/i18n.") and p.endswith(".json"):
            lang = p[len("/i18n."):-len(".json")]
            if lang not in ("ru", "en"):
                self._json({"error": "not found"}, 404)
            else:
                self._file("i18n.%s.json" % lang, "application/json; charset=utf-8")
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
                "auto": st.get("auto", False),
                "scan_mode": st.get("scan_mode") or "full",
                "last_error": st["last_error"],
                "last_cancelled": st.get("last_cancelled", False),
            })
        elif p == "/api/links":
            try:
                self._json(core.audit_links(core.resolve_settings_path()))
            except Exception as e:
                self._json({"path": "", "issues": [], "error": str(e)})
        elif p == "/api/progress":
            with STATE_LOCK:
                st = dict(STATE)
            started = st.get("last_scan_started")
            elapsed = (time.time() - started) if (st["scanning"] and started) else 0.0
            total = st.get("scan_total") or 0
            done = st.get("scan_done") or 0
            self._json({
                "scanning": st["scanning"],
                "total": total,
                "done": done,
                "percent": (round(100.0 * done / total) if total else 0),
                "current": st.get("scan_current"),
                "inflight": st.get("scan_inflight") or [],
                "elapsed": round(elapsed, 1),
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
        elif p == "/api/find-model":
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            query = (q.get("q") or [""])[0]
            deep = (q.get("deep") or ["0"])[0] in ("1", "true", "yes")
            meta = (q.get("meta") or ["0"])[0] in ("1", "true", "yes")
            try:
                hits = locate.find_model(query, deep=deep, include_meta=meta)
            except Exception as e:
                self._json({"error": str(e)}, 500)
                return
            self._json({"query": query, "hits": hits, "count": len(hits)})
        elif p == "/api/summary":
            last = STORE.last_run()
            with STATE_LOCK:
                st = dict(STATE)
            self._json({"state": st, "last_run": last,
                        "recent_runs": STORE.recent_runs(30),
                        "version": core.APP_VERSION,
                        "built": core.build_stamp()})
        elif p == "/api/models":
            rows = STORE.latest_per_model(min_runs=STATE.get("dead_min_runs", 3))
            now = time.time()
            for r in rows:
                r["uptime_24h"] = STORE.uptime(r["model"], r["host"], now - 86400)
                r["uptime_1h"] = STORE.uptime(r["model"], r["host"], now - 3600)
                r["note_code"] = core.classify_note(
                    r.get("status"), r.get("http_code"), r.get("error"))
                r["note_text"] = core.note_text(r.get("error"))
                r["tpm_limit"] = core.otpm_limit(r.get("error"))
                r["wait_s"] = int(r.get("wait_s") or 0)
                r["wait_kind"] = r.get("wait_kind") or ""
                r["ready_ts"] = (r.get("ts") or 0) + r["wait_s"] if r["wait_s"] else 0
            self._json({"models": rows})
        elif p.startswith("/api/model/"):
            from urllib.parse import unquote, parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            model = unquote(p[len("/api/model/"):])
            host = (q.get("host") or [""])[0]
            probes = STORE.model_detail(model, host)
            for pr in probes:
                pr["note_code"] = core.classify_note(
                    pr.get("status"), pr.get("http_code"), pr.get("error"))
                pr["note_text"] = core.note_text(pr.get("error"))
                pr["tpm_limit"] = core.otpm_limit(pr.get("error"))
                pr["wait_s"] = int(pr.get("wait_s") or 0)
                pr["wait_kind"] = pr.get("wait_kind") or ""
                pr["ready_ts"] = (pr.get("ts") or 0) + pr["wait_s"] if pr["wait_s"] else 0
            self._json({"model": model, "host": host,
                        "probes": probes,
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
            # a one-shot scan may override the mode for this pass only; the
            # saved preference is left alone
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                data = {}
            override = data.get("mode")
            threading.Thread(target=do_scan, args=("manual", override),
                             daemon=True).start()
            self._json({"ok": True, "started": True})
        elif p == "/api/auto":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            with STATE_LOCK:
                if "on" in data:
                    STATE["auto"] = bool(data.get("on"))
                state = STATE["auto"]
            if state:
                AUTO.set()
            else:
                AUTO.clear()
            AUTO_WAKE.set()
            self._json({"ok": True, "auto": state})
        elif p == "/api/cancel":
            with STATE_LOCK:
                scanning = STATE["scanning"]
            if not scanning:
                self._json({"ok": False, "reason": "not scanning"})
                return
            CANCEL.set()
            self._json({"ok": True, "cancelling": True})
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
                    STATE["max_tokens"] = max(1, min(8192, int(data["max_tokens"])))
                if isinstance(data.get("timeout"), (int, float)):
                    STATE["timeout"] = max(5.0, min(120.0, float(data["timeout"])))
                if "settings_path" in data:
                    STATE["settings_override"] = str(data.get("settings_path") or "").strip()
                if data.get("language") in ("ru", "en"):
                    STATE["language"] = data["language"]
                if data.get("theme") in ("light", "dark", "system"):
                    STATE["theme"] = data["theme"]
                if data.get("scan_mode") in core.SCAN_MODES:
                    STATE["scan_mode"] = data["scan_mode"]
                st = dict(STATE)
            try:
                core.save_config({
                    "interval_seconds": st["interval"],
                    "max_tokens": st["max_tokens"],
                    "timeout_seconds": st["timeout"],
                    "settings_path": st["settings_override"],
                    "language": st["language"],
                    "theme": st["theme"],
                    "scan_mode": st["scan_mode"],
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
        elif p == "/api/hide":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            model = str(data.get("model") or "").strip()
            host = str(data.get("host") or "").strip()
            if not model or not host:
                self._json({"error": "model and host required"}, 400)
                return
            STORE.hide(model, host,
                       reason_code=str(data.get("reason_code") or ""),
                       reason_text=str(data.get("reason_text") or ""),
                       provider_id=str(data.get("provider_id") or ""))
            self._json({"ok": True, "model": model, "host": host, "hidden": True})
        elif p == "/api/unhide":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            model = str(data.get("model") or "").strip()
            host = str(data.get("host") or "").strip()
            if not model or not host:
                self._json({"error": "model and host required"}, 400)
                return
            changed = STORE.unhide(model, host)
            self._json({"ok": True, "model": model, "host": host,
                        "hidden": False, "existed": changed})
        elif p == "/api/delete-model":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            model = str(data.get("model") or "").strip()
            provider_id = str(data.get("provider_id") or "").strip()
            host = str(data.get("host") or "").strip()
            source_id = str(data.get("source_id") or "").strip()
            if not model:
                self._json({"error": "model required"}, 400)
                return
            if source_id:
                # edit whichever agent the client picked, by its own format
                try:
                    src = core.resolve_source(source_id)
                except Exception as e:
                    self._json({"error": str(e)}, 500)
                    return
                if src is None or src.get("id") != source_id:
                    self._json({"error": "unknown source: %s" % source_id}, 404)
                    return
                res = core.remove_model_in_source(src, provider_id, model)
            else:
                # legacy single-file path: the agent settings json
                res = core.remove_model(core.resolve_settings_path(),
                                        provider_id, model)
            if res.get("ok") and host:
                # the file change alone leaves the row in history; mute it too
                # so the dashboard list actually gets cleaner, while its probe
                # history survives under the "hidden" filter
                try:
                    STORE.hide(model, host, reason_code="removed_from_agent",
                               reason_text="removed from the agent's list",
                               provider_id=provider_id)
                except Exception:
                    pass
            self._json(res, 200 if res.get("ok") else 409)
        elif p in ("/api/fix-link", "/api/drop-provider"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            pid = str(data.get("provider_id") or "").strip()
            if not pid:
                self._json({"error": "provider_id required"}, 400)
                return
            path = core.resolve_settings_path()
            if p == "/api/fix-link":
                # the client approved this exact replacement shown in the row
                res = core.fix_base_url(path, pid, str(data.get("new_base") or ""))
            else:
                res = core.remove_provider(path, pid)
            self._json(res, 200 if res.get("ok") else 409)
        else:
            self._json({"error": "not found"}, 404)


def _open_dashboard(url):
    """Best-effort: show the running dashboard in the user's browser."""
    try:
        exe, fam = report.browser_exe()
    except Exception:
        exe, fam = "", ""
    if exe:
        flag = "-new-window" if fam == "firefox" else "--new-window"
        for cmd in ([exe, flag, url], [exe, url]):
            try:
                subprocess.Popen(cmd)
                return
            except Exception:
                continue
    try:
        if hasattr(os, "startfile"):
            os.startfile(url)  # noqa: S606
            return
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def _port_busy(host, port):
    """True when someone already listens on host:port.

    A plain bind is not enough on Windows: SO_REUSEADDR lets a second process
    take the same port and then silently never receive a connection, which is
    exactly the kind of quiet failure a double-clicked window cannot explain.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(1.0)
        return s.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def _sibling_pids():
    """PIDs of other running copies of this exe (Windows only).

    Matched by image name, so a stale copy launched from any folder is caught.
    Frozen-only by contract: from source sys.executable is python.exe, and
    terminating every python.exe on the box would be absurd.
    """
    if not sys.platform.startswith("win"):
        return []
    import ctypes
    from ctypes import wintypes
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    me = os.getpid()
    name = os.path.normcase(os.path.basename(sys.executable))
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID:
        return []
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = entry.th32ProcessID
            if pid and pid != me \
                    and os.path.normcase(entry.szExeFile) == name:
                out.append(pid)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def _kill_siblings():
    """Terminate every other copy of this exe, so exactly one dashboard is live.

    A stale copy keeps the port bound and serves its old code (which is how an
    outdated build ends up silently shadowing a fresh one). Frozen-only: from
    source the image is python.exe and killing every python.exe would be wild.
    Returns the PIDs actually terminated.
    """
    if not core.FROZEN or not sys.platform.startswith("win"):
        return []
    import ctypes
    PROCESS_TERMINATE = 0x0001
    k32 = ctypes.windll.kernel32
    killed = []
    for pid in _sibling_pids():
        h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not h:
            continue
        try:
            if k32.TerminateProcess(h, 0):
                killed.append(pid)
        finally:
            k32.CloseHandle(h)
    return killed


def _fatal(text):
    """A windowed exe has no console: log the reason and show a message box.

    Without this a failed launch (port taken, no writable folder) would be
    completely silent.
    """
    try:
        print(text)
    except Exception:
        pass
    try:
        d = os.path.join(core.APP_DIR, "data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "modelpulse.log"), "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                   text.replace("\n", " | ")))
    except Exception:
        pass
    if core.FROZEN:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "ModelPulse", 0x30)
        except Exception:
            pass


def main():
    # windowed builds (--noconsole) have no std streams at all; give them a
    # sink so a stray print can never take the server down
    if core.FROZEN:
        for name in ("stdout", "stderr"):
            if getattr(sys, name, None) is None:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--interval", type=int, default=900)
    ap.add_argument("--scan-on-start", action="store_true",
                    help="probe once at launch and turn continuous scanning on")
    ap.add_argument("--quick", dest="scan_mode", action="store_const", const="quick",
                    help="use the fast one-word probe (instant, less reliable)")
    ap.add_argument("--full", dest="scan_mode", action="store_const", const="full",
                    help="use the chat-sized probe (slower, honest)")
    # a double-clicked exe has no console to read, so it opens the panel itself;
    # running from source keeps the old quiet behaviour
    ap.add_argument("--open", dest="open_browser", action="store_true",
                    default=core.FROZEN,
                    help="open the dashboard in the browser at launch")
    ap.add_argument("--no-open", dest="open_browser", action="store_false",
                    help="never open a browser")
    args = ap.parse_args()

    global STORE
    STORE = storage.Store()
    cfg = core.load_config()
    with STATE_LOCK:
        STATE["interval"] = max(60, int(cfg.get("interval_seconds") or args.interval))
        STATE["max_tokens"] = max(1, int(cfg.get("max_tokens") or core.DEFAULT_MAX_TOKENS))
        STATE["timeout"] = max(5.0, float(cfg.get("timeout_seconds") or 30))
        STATE["language"] = cfg.get("language") or "ru"
        STATE["theme"] = cfg.get("theme") or "system"
        mode = args.scan_mode or cfg.get("scan_mode") or "full"
        STATE["scan_mode"] = mode if mode in core.SCAN_MODES else "full"
        STATE["dead_min_runs"] = max(1, int(cfg.get("dead_min_runs") or 3))
        STATE["settings_override"] = (cfg.get("settings_path") or "").strip()
        STATE["active_source"] = (cfg.get("active_source") or "qoder").strip()

    threading.Thread(target=scheduler_loop, daemon=True).start()

    # by default nothing scans until asked: the scheduler is parked and no
    # pass runs at launch. --scan-on-start turns continuity on instead.
    if args.scan_on_start:
        with STATE_LOCK:
            STATE["auto"] = True
        AUTO.set()

    # a fresh launch takes over: kill every other copy of this exe unconditionally
    # (frozen only), so an older build can never keep the port and shadow the new
    # one. Then wait briefly for the socket to free.
    killed = _kill_siblings()
    if killed:
        for _ in range(20):
            time.sleep(0.15)
            if not _port_busy(args.host, args.port):
                break
    if _port_busy(args.host, args.port):
        _fatal("Port %d is already in use - another program is listening.\n"
               "Open http://%s:%d , or close the other window and start again."
               % (args.port, args.host, args.port))
        return 1
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        _fatal("Cannot start the server on port %d: %s" % (args.port, e))
        return 1
    url = "http://%s:%d" % (args.host, args.port)
    print("ModelPulse on %s  (interval %ds)" % (url, STATE["interval"]))
    # the window opens a moment later so the socket is already listening
    if args.open_browser:
        threading.Timer(1.0, _open_dashboard, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
