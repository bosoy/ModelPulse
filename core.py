#!/usr/bin/env python3
"""ModelPulse core: read the BYOK providers your agent knows about and probe them.

The agent's model registry IS the settings file (default ~/.qoder/settings.json)
-> "providers". Both the agent tool and this app read the same file; no IPC is
involved. Source is ASCII-only on purpose (Python 3.14 on Windows reads .py as
cp1251).
"""

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

DEFAULT_SETTINGS = os.path.join(os.path.expanduser("~"), ".qoder", "settings.json")
DEFAULT_TIMEOUT = 30.0
DEFAULT_WORKERS = 8
PROMPT = "Reply with exactly one word: pong"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

_DEFAULT_CONFIG = {
    "settings_path": "",
    "interval_seconds": 900,
    "max_tokens": 16,
    "timeout_seconds": 30,
    "workers": 8,
    "language": "ru",
    "theme": "system",
    "browser": "",
    "active_source": "qoder",
}


def load_config():
    cfg = dict(_DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for k in _DEFAULT_CONFIG:
                if k in data and data[k] not in (None, ""):
                    cfg[k] = data[k]
                elif k in data and k == "settings_path":
                    cfg[k] = data[k]
    except Exception:
        pass
    return cfg


def save_config(cfg):
    cur = load_config()
    cur.update(cfg)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, indent=2, ensure_ascii=False)
    return cur


def resolve_settings_path(explicit=None):
    """Priority: explicit arg > env > config.json > default."""
    if explicit:
        return explicit
    env = os.environ.get("MODELPULSE_SETTINGS")
    if env:
        return env
    cfg = load_config()
    sp = (cfg.get("settings_path") or "").strip()
    if sp:
        return sp if os.path.isabs(sp) else os.path.join(APP_DIR, sp)
    return DEFAULT_SETTINGS


def settings_path():
    return resolve_settings_path()


def load_targets(path=None):
    """Return (path, mtime, targets). Retries briefly: the agent may be writing."""
    path = path or resolve_settings_path()
    last_err = None
    for attempt in range(4):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            mtime = os.path.getmtime(path)
            break
        except Exception as e:
            last_err = e
            time.sleep(0.25 * (attempt + 1))
    else:
        raise RuntimeError("cannot read %s: %s" % (path, last_err))

    provs = data.get("providers") or {}
    targets = []
    seen = set()
    for pid, obj in provs.items():
        base = (obj.get("baseUrl") or "").strip()
        if not base:
            continue
        root = api_root(base)
        key = (obj.get("apiKey") or "").strip()
        models = []
        for m in obj.get("models") or []:
            mid = m.get("model") or m.get("displayName")
            if mid:
                models.append(mid)
        if not models and obj.get("model"):
            models.append(obj["model"])
        for mid in models:
            sig = (root, key, mid)
            if sig in seen:
                continue
            seen.add(sig)
            targets.append({
                "provider_id": pid,
                "root": root,
                "host": host_of(root),
                "key": key,
                "model": mid,
            })
    return path, mtime, targets


def api_root(base):
    b = base.strip().rstrip("/")
    for suf in ("/chat/completions", "/models"):
        if b.endswith(suf):
            b = b[: -len(suf)]
    return b.rstrip("/")


def host_of(base):
    try:
        return base.split("//", 1)[1].split("/", 1)[0]
    except Exception:
        return base


# -- multi-source dispatch ---------------------------------------------
def _import_discover():
    import discover
    return discover


def list_sources(deep=False):
    """Return discovered agent sources (no secrets inside)."""
    return _import_discover().discover(deep=deep)


def resolve_source(source_id, deep=False):
    """Find a discovered source by id. None -> active from config, else qoder."""
    source_id = (source_id or "").strip()
    if not source_id:
        source_id = (load_config().get("active_source") or "qoder").strip()
    sources = list_sources(deep=deep)
    for s in sources:
        if s["id"] == source_id:
            return s
    return sources[0] if sources else None


def load_targets_for(source_id=None, deep=False):
    """Return (source, path, mtime, targets) for a source id.

    The classic Qoder source keeps using load_targets() so its exact retry and
    parsing behaviour is preserved. Other sources go through discover.
    """
    src = resolve_source(source_id, deep=deep)
    if src is None:
        path, mtime, targets = load_targets()
        return None, path, mtime, targets
    if src["id"] == "qoder" or src.get("format") == "qoder":
        path, mtime, targets = load_targets()
        return src, path, mtime, targets
    disc = _import_discover()
    targets = disc.targets_for_source(src)
    return src, src.get("path"), src.get("mtime"), targets


def http_json(url, key, payload=None, timeout=DEFAULT_TIMEOUT):
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        return e.code, _maybe_json(raw), raw[:200]
    except Exception as e:
        return None, None, type(e).__name__ + ": " + str(e)[:200]
    return code, _maybe_json(body), None


def _maybe_json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def extract_text(body):
    if not isinstance(body, dict):
        return None, ""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, ""
    ch = choices[0] or {}
    msg = ch.get("message") or {}
    content = msg.get("content")
    if content is None:
        content = ch.get("text")
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("text"):
                parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        content = "".join(parts)
    text = (content or "").strip()
    if text:
        return text, ""
    rc = msg.get("reasoning_content") or msg.get("reasoning") or ""
    fr = ch.get("finish_reason") or ""
    if rc:
        return None, "reasoning only (no answer text)"
    if fr:
        return None, "empty content, finish=%s" % fr
    return None, "no text"


def probe(target, timeout=DEFAULT_TIMEOUT, max_tokens=16):
    url = target["root"] + "/chat/completions"
    payload = {
        "model": target["model"],
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    t0 = time.time()
    code, body, err = http_json(url, target["key"], payload, timeout)
    ms = int((time.time() - t0) * 1000)

    status = "fail"
    reply = ""
    error = ""
    if code == 200 and body is not None:
        text, note = extract_text(body)
        if text:
            status = "reply"
            reply = text.replace("\n", " ")[:120]
        else:
            status = "empty"
            error = note or "no text"
    elif code is None:
        error = err or "network error"
    else:
        error = err or ""
        if not error and isinstance(body, dict):
            error = str(body.get("error") or body)[:200]
    return {
        "model": target["model"],
        "host": target["host"],
        "provider_id": target["provider_id"],
        "status": status,
        "http_code": code,
        "ms": ms,
        "reply": reply,
        "error": error,
    }


def scan(targets, timeout=DEFAULT_TIMEOUT, workers=DEFAULT_WORKERS, max_tokens=16,
         should_stop=None):
    """Probe targets in parallel. should_stop() -> True aborts the rest.

    Cancellation is checked between completions (every ~0.25s), so it is prompt
    from the caller's view. Requests already in flight cannot be killed cleanly
    and finish in the background; their results are simply dropped.
    """
    if not targets:
        return []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        pending = {pool.submit(probe, t, timeout, max_tokens) for t in targets}
        results = []
        while pending:
            if should_stop is not None and should_stop():
                for f in pending:
                    f.cancel()
                break
            done, pending = wait(pending, timeout=0.25,
                                 return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    results.append(f.result())
                except Exception:
                    pass
        return results
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def summarize(results):
    replied = sum(1 for r in results if r["status"] == "reply")
    empty = sum(1 for r in results if r["status"] == "empty")
    return {
        "total": len(results),
        "replied": replied,
        "empty": empty,
        "failed": len(results) - replied - empty,
    }
