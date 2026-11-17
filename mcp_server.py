#!/usr/bin/env python3
"""ModelPulse MCP server: expose the probe as agent tools.

Speaks MCP over stdio (newline-delimited JSON-RPC). Reads the BYOK providers
from the same registry core.py uses and actively probes them, so an agent can
answer "which models are online right now" with real latency and a real reply,
not a static config flag.

Two tools:
  models_status   - mode=ping: representative long-answer chat call per model
                    (REPLY/EMPTY/FAIL); mode=list: GET {base}/models per provider
  models_providers - offline inventory of configured providers

No third-party deps: stdlib only. Keys are read into memory and never written
to disk or emitted in tool output. Source is intentionally ASCII-only because
Python 3.14 on Windows parses .py as cp1251.
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import core

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "modelpulse"
SERVER_VERSION = "1.0.0"
MAX_WORKERS = 8


def log(msg):
    sys.stderr.write("[modelpulse] " + str(msg) + "\n")
    sys.stderr.flush()


def _probe_lang():
    """Probe in the dashboard's language, so the tool reports answers the user
    can read back. Falls back to English when config.json has no language."""
    try:
        lang = (core.load_config().get("language") or "en").strip().lower()
    except Exception:
        lang = "en"
    return "ru" if lang.startswith("ru") else "en"


def ping_mode(targets, timeout):
    lang = _probe_lang()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(
            lambda t: core.probe(t, timeout=timeout,
                                 max_tokens=core.DEFAULT_MAX_TOKENS, lang=lang),
            targets))
    rank = {"reply": 0, "empty": 1, "fail": 2}
    results.sort(key=lambda r: (rank.get(r["status"], 3), r["host"], r["model"]))
    return results

def list_mode(targets, timeout):
    def one(t):
        url = t["root"] + "/models"
        t0 = time.time()
        code, body, err, _ = core.http_json(url, t["key"], None, timeout)
        ms = int((time.time() - t0) * 1000)
        ids = []
        if code == 200 and isinstance(body, dict):
            for m in body.get("data") or []:
                mid = m.get("id") if isinstance(m, dict) else None
                if mid:
                    ids.append(mid)
        return {
            "model": t["model"], "host": t["host"], "ok": code == 200,
            "code": code, "ms": ms, "count": len(ids),
            "has_model": t["model"] in ids, "error": err or "",
        }
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(one, targets))
    results.sort(key=lambda r: (not r["ok"], r["host"], r["model"]))
    return results


def render_ping(path, results):
    total = len(results)
    replied = sum(1 for r in results if r["status"] == "reply")
    empty = sum(1 for r in results if r["status"] == "empty")
    errs = total - replied - empty
    lines = ["modelpulse: mode=ping  models=%d  replied=%d  empty=%d  failed=%d"
             % (total, replied, empty, errs),
             "source: " + path, "",
             "%-7s %-30s %-28s %6s  %s" % ("ST", "MODEL", "HOST", "MS", "REPLY / NOTE"),
             "-" * 104]
    for r in results:
        st = {"reply": "REPLY", "empty": "EMPTY", "fail": "FAIL"}.get(r["status"], "?")
        if r["status"] == "reply":
            note = r["reply"]
        elif r["status"] == "empty":
            note = r["error"] or "no text"
        else:
            note = ("HTTP %s" % r["http_code"] if r["http_code"] else (r["error"] or "err"))
        lines.append("%-7s %-30s %-28s %6d  %s"
                     % (st, r["model"][:30], r["host"][:28], r["ms"], note[:62]))
    lines.append("")
    lines.append("REPLY = model returned actual text. EMPTY = HTTP 200 but no "
                 "answer text (reasoning-only/truncated). FAIL = error.")
    return "\n".join(lines)


def render_list(path, results):
    total = len(results)
    up = sum(1 for r in results if r["ok"])
    lines = ["modelpulse: mode=list  models=%d  online=%d  offline=%d"
             % (total, up, total - up),
             "source: " + path, "",
             "%-6s %-30s %-28s %6s %8s %s"
             % ("ST", "MODEL", "HOST", "MS", "MODELS", "IN-LIST?"),
             "-" * 100]
    for r in results:
        st = "UP" if r["ok"] else "DOWN"
        present = ("yes" if r["has_model"] else "no") if r["ok"] else "-"
        cnt = str(r["count"]) if r["ok"] else "-"
        note = "" if r["ok"] else ("HTTP %s" % r["code"] if r["code"] else (r["error"] or "err"))
        lines.append("%-6s %-30s %-28s %6d %8s %-8s %s"
                     % (st, r["model"][:30], r["host"][:28], r["ms"], cnt, present, note[:40]))
    lines.append("")
    lines.append("UP = HTTP 200 on GET /models. 'ping' mode proves a real reply.")
    return "\n".join(lines)


def tool_providers():
    path, _mtime, targets = core.load_targets()
    by_pid = {}
    for t in targets:
        by_pid.setdefault((t["provider_id"], t["root"]), []).append(t["model"])
    lines = ["providers=%d  models=%d" % (len(by_pid), len(targets)),
             "source: " + path, ""]
    for (pid, root), models in sorted(by_pid.items(), key=lambda kv: kv[1]):
        lines.append("%s  (%s)" % (core.host_of(root), pid))
        for m in models:
            lines.append("    - " + m)
    return "\n".join(lines)


def tool_status(args):
    mode = (args.get("mode") or "ping").lower()
    if mode not in ("ping", "list"):
        return "error: mode must be 'ping' or 'list'"
    filt = (args.get("filter") or "").strip()
    try:
        timeout = float(args.get("timeout") or core.DEFAULT_TIMEOUT)
    except Exception:
        timeout = core.DEFAULT_TIMEOUT
    path, _mtime, targets = core.load_targets()
    if filt:
        f = filt.lower()
        targets = [t for t in targets
                   if f in t["model"].lower() or f in t["provider_id"].lower()
                   or f in t["host"].lower()]
    if not targets:
        return "no providers matched (filter=%r). Try mode=list or drop the filter." % filt
    if mode == "ping":
        return render_ping(path, ping_mode(targets, timeout))
    return render_list(path, list_mode(targets, timeout))


TOOLS = [
    {
        "name": "models_status",
        "description": ("Actively probe the BYOK models configured in your agent and "
                        "report which actually answer. mode=ping asks each model for "
                        "a representative multi-sentence answer (so a model that only "
                        "survives a one-word ping but dies on a real chat message is "
                        "reported unavailable) and only counts a real non-empty reply "
                        "as success (REPLY / EMPTY / FAIL); mode=list calls GET "
                        "/models per provider (no generation). Optional filter is "
                        "a substring on model id, provider id or host."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["ping", "list"],
                         "default": "ping",
                         "description": "ping = live chat-sized call; list = fetch /models"},
                "filter": {"type": "string",
                           "description": "substring match on model id or provider id"},
                "timeout": {"type": "number", "default": core.DEFAULT_TIMEOUT,
                            "description": "per-request timeout in seconds"},
            },
        },
    },
    {
        "name": "models_providers",
        "description": ("List the BYOK providers and models configured in your agent "
                        "without any network call (offline inventory)."),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call_tool(name, args):
    if name == "models_providers":
        return tool_providers()
    if name == "models_status":
        return tool_status(args)
    return "error: unknown tool " + name


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            text = call_tool(name, args)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "error: " + str(e)}],
                "isError": True}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "method not found: " + str(method)}}
    return None


def main():
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            log("bad json: " + str(e))
            continue
        resp = handle(msg)
        if resp is not None:
            send(resp)


if __name__ == "__main__":
    main()
