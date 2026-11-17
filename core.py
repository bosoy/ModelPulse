#!/usr/bin/env python3
"""ModelPulse core: read the BYOK providers your agent knows about and probe them.

The agent's model registry IS the settings file (default ~/.qoder/settings.json)
-> "providers". Both the agent tool and this app read the same file; no IPC is
involved. Source is ASCII-only on purpose (Python 3.14 on Windows reads .py as
cp1251).
"""

import datetime
import email.utils
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

DEFAULT_SETTINGS = os.path.join(os.path.expanduser("~"), ".qoder", "settings.json")
DEFAULT_TIMEOUT = 30.0
DEFAULT_WORKERS = 8
DEFAULT_MAX_TOKENS = 4096
# Fallback probe text (English). The live wording comes from the UI language
# pack via prompt_for(), so a Russian interface gets Russian answers back and
# this source stays ASCII-only.
PROMPT_QUICK = "Reply with exactly one word: pong"
PROMPT_FULL = ("Write a detailed answer of about 300 words explaining how a web browser "
               "loads a page, including DNS, TLS, HTTP, rendering, and caching.")

# Two probe strengths. "quick" is one word: instant and cheap, but a model that
# only survives a one-word ping (e.g. metered output tokens per minute) can look
# alive here and die in chat. "full" asks a real chat-sized answer, so it is the
# honest verdict - and the slower one.
SCAN_MODES = ("quick", "full")
PROMPT_BY_MODE = {"quick": PROMPT_QUICK, "full": PROMPT_FULL}

_PROMPT_CACHE = {}


def prompt_for(mode="full", lang="en"):
    """The probe text for a mode, in the dashboard's language when available.

    The wording lives in the UI language pack (probe_quick / probe_full) so a
    Russian interface gets Russian answers back; this module stays ASCII-only
    and falls back to the English constants if a pack is missing or has no key.
    """
    fallback = PROMPT_BY_MODE.get(mode, PROMPT_FULL)
    lang = "ru" if str(lang).lower().startswith("ru") else "en"
    key = "probe_" + ("quick" if mode == "quick" else "full")
    if lang not in _PROMPT_CACHE:
        pack = {}
        try:
            with open(os.path.join(RES_DIR, "ui", "i18n.%s.json" % lang),
                      "r", encoding="utf-8") as fh:
                pack = json.load(fh)
        except Exception:
            pack = {}
        _PROMPT_CACHE[lang] = pack
    return _PROMPT_CACHE[lang].get(key) or fallback

# Keep in sync with the git tag (vX.Y.Z). Shown in the dashboard header.
APP_VERSION = "1.0.0"

# Cloudflare-fronted hosts ban the default Python-urllib signature (error 1010),
# so send a plain browser UA; without it whole providers look "down" when they are up.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

FROZEN = getattr(sys, "frozen", False)

# Frozen one-file builds have two roots: resources the exe carries inside
# itself (ui/, lang packs) unpack to a temp dir that is wiped on exit, so
# writable state (config.json, data/) must sit next to the exe instead.
# Running from source, both roots are the script's own folder.
if FROZEN:
    RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    RES_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = RES_DIR

CONFIG_PATH = os.path.join(APP_DIR, "config.json")


def build_stamp():
    """Date this build was made: the exe's mtime when frozen, else core.py's."""
    try:
        target = sys.executable if FROZEN else os.path.abspath(__file__)
        return datetime.date.fromtimestamp(os.path.getmtime(target)).isoformat()
    except Exception:
        return ""

_DEFAULT_CONFIG = {
    "settings_path": "",
    "interval_seconds": 900,
    "max_tokens": 4096,
    "timeout_seconds": 30,
    "workers": 8,
    "language": "ru",
    "theme": "system",
    "browser": "",
    "active_source": "qoder",
    "scan_mode": "full",
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
        # keep each model's own output budget: the agent asks for maxOutputTokens,
        # so probing at a smaller cap can pass a model the chat will 429 on. None
        # means "no budget stated" -> probe at the shared default.
        models = []
        for m in obj.get("models") or []:
            mid = m.get("model") or m.get("displayName")
            if mid:
                models.append((mid, _model_token_budget(m)))
        if not models and obj.get("model"):
            models.append((obj["model"], None))
        for mid, budget in models:
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
                "max_tokens": budget,
            })
    return path, mtime, targets


def _model_token_budget(entry):
    """maxOutputTokens from one settings.json model entry, as a positive int.

    None when the entry does not state one (or states a junk value), so the
    caller probes at the shared default instead of inventing a cap.
    """
    if not isinstance(entry, dict):
        return None
    for k in ("maxOutputTokens", "max_output_tokens", "maxTokens"):
        v = entry.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    return None


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


def audit_links(path=None):
    """Report provider baseUrls that cannot serve chat/completions.

    A baseUrl ending in /models (or /chat/completions) is a listing/endpoint
    URL pasted into the base field: the agent appends its own path and gets
    404. api_root() silently repairs it for probing, which is exactly why such
    a provider looks fine in the dashboard yet never answers in chat.
    Returns issues with the exact address and a suggested replacement.
    No keys are ever returned.
    """
    path = path or resolve_settings_path()
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8-sig"))
    except Exception as e:
        return {"path": path, "issues": [], "error": str(e)}
    provs = data.get("providers") or {}
    issues = []
    roots = {}
    for pid, obj in provs.items():
        if not isinstance(obj, dict):
            continue
        base = (obj.get("baseUrl") or "").strip()
        if not base:
            continue
        models = [m for m in (_model_entry_id(x) for x in (obj.get("models") or []))
                  if m]
        if not models and obj.get("model"):
            models = [obj["model"]]
        kind = ""
        if base.rstrip("/").endswith("/chat/completions"):
            kind = "endpoint_url"
        elif base.rstrip("/").endswith("/models"):
            kind = "list_url"
        root = api_root(base)
        sig = (root, (obj.get("apiKey") or "").strip(), tuple(sorted(models)))
        dup = roots.get(sig)
        roots.setdefault(sig, pid)
        if kind or dup:
            issues.append({
                "provider_id": pid,
                "base_url": base,
                "root": root,
                "models": models,
                "kind": kind or "duplicate",
                "suggested": root if kind else "",
                "duplicate_of": dup or "",
                "path": path,
            })
    return {"path": path, "issues": issues}


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
    """-> (code, parsed_body, error_text, response_headers).

    Headers are kept because they carry the exact moment a throttled model
    becomes usable again (Retry-After, x-ratelimit-reset-*).
    """
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json",
               "User-Agent": USER_AGENT}
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
            got = dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        # keep enough of the body for the stated token cap to survive: a long
        # organization id used to push "Limit 1000" past the cut
        return e.code, _maybe_json(raw), raw[:600], dict(e.headers or {})
    except Exception as e:
        return None, None, type(e).__name__ + ": " + str(e)[:200], {}
    return code, _maybe_json(body), None, got


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


_OTPM_RE = re.compile(
    r"output tokens per minute[^.]*?Limit[: ]+(\d+)", re.I)
_LIMIT_RE = re.compile(r"Limit[: ]+(\d+)", re.I)


def otpm_limit(error):
    """Groq-style 'Request too large ... output tokens per minute (OTPM):
    Limit 1000' -> 1000. Zero when the body carries no such cap."""
    s = error or ""
    if "too large" not in s.lower():
        return 0
    m = _OTPM_RE.search(s) or _LIMIT_RE.search(s)
    try:
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


# --- when a throttled model becomes usable again --------------------------

_DUR_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(ms|minutes?|mins?|hours?|hrs?|seconds?|secs?|h|m|s)"
    r"(?![a-z])", re.I)
_UNIT_S = {"ms": 0.001, "h": 3600.0, "hr": 3600.0, "hrs": 3600.0,
           "hour": 3600.0, "hours": 3600.0, "m": 60.0, "min": 60.0,
           "mins": 60.0, "minute": 60.0, "minutes": 60.0, "s": 1.0,
           "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0}
_MAX_WAIT_S = 31 * 86400


def _duration_seconds(text):
    """'1m26.4s', '622ms', '4h 33m', '2 h 42 min' -> seconds."""
    total = 0.0
    for num, unit in _DUR_RE.findall(text or ""):
        try:
            total += float(num) * _UNIT_S.get(unit.lower(), 0.0)
        except ValueError:
            continue
    return total


def _seconds_until_utc(hour, minute):
    now = datetime.datetime.now(datetime.timezone.utc)
    nxt = now.replace(hour=hour % 24, minute=minute % 60, second=0, microsecond=0)
    if nxt <= now:
        nxt += datetime.timedelta(days=1)
    return (nxt - now).total_seconds()


def _header_seconds(value):
    """One rate-limit header value: seconds, a duration, or an absolute date."""
    v = (value or "").strip()
    if not v:
        return 0.0
    if re.fullmatch(r"\d+(?:\.\d+)?", v):
        return float(v)
    d = _duration_seconds(v)
    if d > 0:
        return d
    try:
        dt = email.utils.parsedate_to_datetime(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        pass
    m = re.match(r"(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)", v)
    if m:
        try:
            dt = datetime.datetime.strptime(
                m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=datetime.timezone.utc)
            return max(0.0, dt.timestamp() - time.time())
        except ValueError:
            return 0.0
    return 0.0


def header_wait(headers):
    """Seconds the provider itself asks us to wait; 0 when it says nothing.

    Retry-After is the explicit instruction and wins. Otherwise both reset
    windows matter (requests AND tokens), so take the longer one.
    """
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    ms = h.get("retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000.0)
        except (TypeError, ValueError):
            pass
    s = _header_seconds(h.get("retry-after"))
    if s > 0:
        return s
    resets = [_header_seconds(h[k]) for k in
              ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
               "x-ratelimit-reset") if k in h]
    resets = [x for x in resets if x > 0]
    return max(resets) if resets else 0.0


_TEXT_WAIT_RES = (
    re.compile(r"resets?\s+in\s+([0-9]+\s*[hms][^.]{0,24})", re.I),
    re.compile(r"in\s+(\d+\s*h\s*\d+\s*min)", re.I),
    re.compile(r"(?:try again|retry|wait|available again|back)\s+in\s+"
               r"([0-9]+\s*[hms][^.]{0,24})", re.I),
    re.compile(r"retry[- ]after\s*:?\s*(\d+)", re.I),
)
_RESET_AT_RE = re.compile(r"resets?\s+at\s+(\d{1,2}):(\d{2})", re.I)
_WINDOW_RE = re.compile(
    r"per\s+(\d+)?\s*(second|minute|hour|day|min|sec)s?", re.I)
_WINDOW_S = {"second": 1.0, "sec": 1.0, "minute": 60.0, "min": 60.0,
             "hour": 3600.0, "day": 86400.0}


def text_wait(error):
    """Seconds until availability, read out of the provider's own words.

    Covers 'It resets at 00:00 UTC, in 2 h 42 min', 'resets in 4h 33m',
    'Rate limit reached: 5 requests per 1 minute(s)', 'try again in a few
    seconds'. A bare per-minute window is an upper bound, not an exact wait.
    """
    s = note_text(error) or (error or "")
    if not s:
        return 0.0
    for rx in _TEXT_WAIT_RES:
        m = rx.search(s)
        if m:
            d = _duration_seconds(m.group(1))
            if d > 0:
                return d
    m = _RESET_AT_RE.search(s)
    if m:
        return _seconds_until_utc(int(m.group(1)), int(m.group(2)))
    if re.search(r"in a few seconds", s, re.I):
        return 5.0
    m = _WINDOW_RE.search(s)
    if m:
        try:
            n = float(m.group(1)) if m.group(1) else 1.0
        except ValueError:
            n = 1.0
        return n * _WINDOW_S.get(m.group(2).lower(), 0.0)
    return 0.0


# only throttles have a meaningful "come back later"; a bad key, a retired
# model or a paid tier never heals on its own
_WAIT_KINDS = ("quota", "limit", "otpm", "capacity", "concurrency")
_RESET_RE = re.compile(r"resets?\s+(?:in|at)", re.I)
_RESET_PROMISE_RE = re.compile(
    r"resets?\s+(?:in|at)|available again|try again in", re.I)


def wait_info(headers, error, status="fail", http_code=None):
    """(seconds, kind) until the model is worth calling again, else (0, '')."""
    if status == "reply":
        return 0, ""
    kind = classify_note(status, http_code, error)
    secs = header_wait(headers) or text_wait(error)
    if kind not in _WAIT_KINDS:
        # a daily free allowance can arrive as 402 "paid"/"denied" while the
        # provider still promises a reset - that promise is the availability
        if secs > 0 and _RESET_PROMISE_RE.search(note_text(error) or ""):
            kind = "quota"
        else:
            return 0, ""
    if secs <= 0 or secs > _MAX_WAIT_S:
        return 0, ""
    return int(round(secs)), kind


def _probe_once(target, url, timeout, max_tokens, mode="full", lang="en"):
    prompt = prompt_for(mode, lang)
    payload = {
        "model": target["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    t0 = time.time()
    code, body, err, hdrs = http_json(url, target["key"], payload, timeout)
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
            error = str(body.get("error") or body)[:600]
    wait_s, wait_kind = wait_info(hdrs, error, status, code)
    return {
        "model": target["model"],
        "host": target["host"],
        "provider_id": target["provider_id"],
        "status": status,
        "http_code": code,
        "ms": ms,
        "reply": reply,
        "error": error,
        "wait_s": wait_s,
        "wait_kind": wait_kind,
        "mode": mode,
    }


def probe(target, timeout=DEFAULT_TIMEOUT, max_tokens=DEFAULT_MAX_TOKENS, mode="full",
          lang="en"):
    """One probe. mode='full' asks a real chat-sized answer (honest, slower);
    mode='quick' asks one word (instant, can miss a model that dies in chat).
    lang picks the prompt wording (the dashboard language), so answers come
    back in the language the user reads.

    The model's own budget from settings.json wins over max_tokens: probed at a
    smaller cap a metered model can answer yet still 429 in chat when the agent
    asks for its full maxOutputTokens, which is exactly the false "green" this
    avoids. Failures are not retried smaller."""
    budget = target.get("max_tokens") if isinstance(target, dict) else None
    cap = budget if isinstance(budget, int) and budget > 0 else max_tokens
    url = target["root"] + "/chat/completions"
    return _probe_once(target, url, timeout, cap, mode, lang)


def scan(targets, timeout=DEFAULT_TIMEOUT, workers=DEFAULT_WORKERS,
         max_tokens=DEFAULT_MAX_TOKENS, should_stop=None, on_result=None,
         mode="full", on_start=None, sort_key=None, lang="en"):
    """Probe targets in parallel. should_stop() -> True aborts the rest.

    Cancellation is checked between completions (every ~0.25s), so it is prompt
    from the caller's view. Requests already in flight cannot be killed cleanly
    and finish in the background; their results are simply dropped - but every
    probe that already completed is returned, so a cancelled pass still yields
    whatever it managed to check.

    on_start(target), if given, is called inside a worker thread the moment a
    probe actually begins its request - not when it is queued - so a caller can
    show which models are genuinely in flight right now. It runs in several
    threads, so the callback must be thread-safe.

    on_result(result), if given, is called in this thread as each probe finishes
    (never concurrently), so a caller can stream live progress - done count,
    the model just checked, its timing. Exceptions in either callback never
    affect the scan.

    sort_key, if given, sorts the targets before probing. Work is submitted to
    the pool in that order and the pool drains its queue in order, so the best
    candidates are checked first - which matters most on a pass the user may
    cancel early.

    mode is passed through to probe(): 'full' (chat-sized) or 'quick' (one word).
    lang picks the prompt wording so answers arrive in the reader's language.
    """
    if not targets:
        return []
    if sort_key is not None:
        targets = sorted(targets, key=sort_key)

    def _run_one(t):
        if on_start is not None:
            try:
                on_start(t)
            except Exception:
                pass
        return probe(t, timeout, max_tokens, mode, lang)

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        pending = {pool.submit(_run_one, t) for t in targets}
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
                    r = f.result()
                except Exception:
                    continue
                results.append(r)
                if on_result is not None:
                    try:
                        on_result(r)
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


# -- dead-model detection ----------------------------------------------
# Reasons a model can be gone for good, as opposed to just rate-limited or
# flaky right now. Only these count toward a "dead" verdict; 429/5xx/timeout
# do not, because they come back on their own.
DEAD_REASONS = {
    "bad_model": "provider does not know this model id (renamed or removed)",
    "no_access": "key rejected / plan exhausted / access closed",
    "disabled": "endpoint paused or in maintenance",
    "gone": "model retired by the provider",
}

_DEAD_DEFAULT_MIN_RUNS = 3


# -- note classification -------------------------------------------------
# Map a raw provider error body to a short stable class name the lang pack
# can translate (note_<code>). The raw text is kept as a tooltip / in the
# drawer, so nothing is lost on disk. Order matters: specific signals win.

def _extract_provider_message(error):
    """A human line out of a raw JSON / HTML error body; may be empty."""
    s = (error or "").strip()
    if not s:
        return ""
    if s.startswith("{"):
        def pick(obj):
            if isinstance(obj, dict):
                for key in ("message", "detail", "title"):
                    v = obj.get(key)
                    if isinstance(v, str):
                        return v
                for v in obj.values():
                    found = pick(v)
                    if found:
                        return found
            elif isinstance(obj, list):
                for v in obj:
                    found = pick(v)
                    if found:
                        return found
            return ""
        try:
            return " ".join(pick(json.loads(s)).split())
        except ValueError:
            # error bodies are often truncated for storage, so json.loads
            # fails; salvage the first human message even on broken JSON
            m = re.search(
                r'"(?:message|detail|title)"\s*:\s*"((?:[^"\\]|\\.)*)', s)
            if m:
                return " ".join(m.group(1).split())
    m = re.search(r"<title>([^<]{0,200})</title>", s, re.I)
    if m:
        return " ".join(m.group(1).split())
    return " ".join(s.split())


def _short_plain_error(s):
    m = re.match(r"^([A-Za-z][A-Za-z ]*Error(?::[^}\"]*)?)", s)
    if m:
        return m.group(1).strip()
    m = re.match(r"^(\d{3})(?:\s|$)", s)
    if m:
        return "HTTP " + m.group(1)
    return ""


def note_text(error):
    """Provider's own message, stripped of JSON / HTML boilerplate."""
    msg = _extract_provider_message(error)
    if msg.startswith("{"):
        return _short_plain_error(msg)
    return re.sub(r"\s*\(request id:[^)]*\)", "", msg).strip()


def classify_note(status, http_code, error):
    """Stable short code for one probe row; '' for a healthy reply."""
    if status == "reply":
        return ""
    err = (error or "").lower()
    msg = note_text(error).lower()
    if status == "empty":
        return "reasoning" if "reasoning" in err else "empty"
    if http_code == 403 and ("error 1010" in err or "error-1010" in err
                             or "browser_signature_banned" in err):
        return "cf_ban"
    if "timed out" in err or "timeout" in err or "deadline" in err:
        return "timeout"
    if ("name or service" in err or "getaddrinfo" in err
            or "connection" in err or "reset by peer" in err
            or "ssl " in err or "<urlopen" in err or "max retries" in err
            or "unreachable" in err or "refused" in err):
        return "network"
    # access / billing intent beats the transient rate-limit pattern
    if "check-in" in err or "check in" in msg:
        return "checkin"
    # a used-up daily allowance arrives as 402/403 yet heals on its own: the
    # provider names a reset, so this is a quota, not a denial
    if _RESET_RE.search(msg) and ("usage" in msg or "used up" in msg):
        return "quota"
    if ("free tier" in msg or "insufficient credit" in msg
            or "insufficient_credit" in err or "balance" in msg
            or "credits" in msg and http_code in (402, 403)):
        return "paid"
    if "quota" in err:
        return "quota"
    # retired / missing model
    if (http_code == 404 or "does not exist" in msg or "unknown model" in msg
            or "not_found" in err or "check the model" in msg
            or "model not found" in msg):
        return "not_found"
    # temporarily disabled or offline
    if ("paused" in msg or "maintenance" in msg or "temporarily disabled" in msg
            or "turned off" in msg or "no longer" in msg or "retired" in msg):
        return "offline"
    if "in flight" in msg or "concurrency" in err:
        return "concurrency"
    if otpm_limit(error):
        return "otpm"
    if "rate limit" in msg or "too many requests" in msg or "rate_limit" in err \
            or "request limit" in msg:
        return "limit"
    if ("capacity" in msg or "overloaded" in err or "server_busy" in err
            or "as many model calls" in msg or "busy" in msg or "peak " in msg):
        return "capacity"
    if http_code == 429:
        return "limit"
    if http_code == 401:
        return "key"
    if http_code in (402, 403):
        return "denied"
    if http_code in (502, 503, 504):
        return "gateway"
    if http_code and 500 <= http_code < 600:
        return "server"
    if http_code:
        return "http"
    return "error"


def classify_probe(http_code, status, error):
    """Map one probe row to a persistent-failure class, or None if it can recover.

    Returns one of DEAD_REASONS keys only for signals that do not heal by
    themselves. 429 / 5xx / timeouts / EMPTY are deliberately not dead: they
    are transient. A Cloudflare browser-signature ban is our own problem, not
    the provider's, so it is excluded too.
    """
    err = (error or "").lower()
    if status == "reply":
        return None
    # our own UA problem, not the provider's fault
    if http_code == 403 and ("browser_signature_banned" in err or "error 1010" in err
                             or "error-1010" in err):
        return None
    if http_code == 404:
        return "bad_model"
    if http_code in (401, 402, 403):
        return "no_access"
    if http_code == 410:
        return "gone"
    if http_code == 400 and ("paused" in err or "maintenance" in err
                             or "disabled" in err or "not available" in err
                             or "no longer" in err or "retired" in err):
        return "disabled"
    return None


def classify_dead(history, min_runs=_DEAD_DEFAULT_MIN_RUNS):
    """Verdict for one (model, host) from its recent probe history.

    history is oldest-first (as storage._history returns it). A model is dead
    only when the last `min_runs` probes in a row all share the same dead
    class; mixed or fewer rows mean "not dead yet". Returns
    (is_dead, reason_code, reason_text).
    """
    if not history or len(history) < min_runs:
        return False, None, ""
    recent = history[-min_runs:]
    codes = [classify_probe(h.get("http_code"), h.get("status"), h.get("error"))
             for h in recent]
    first = codes[0]
    if first and all(c == first for c in codes):
        return True, first, DEAD_REASONS.get(first, first)
    return False, None, ""


# -- delete a model from the agent's own list --------------------------
def _model_entry_id(entry):
    if isinstance(entry, dict):
        return entry.get("model") or entry.get("id") or entry.get("displayName")
    return entry


def _backup_file(path):
    """Copy the target into our own data dir; returns the backup path."""
    bdir = os.path.join(APP_DIR, "data", "backups")
    os.makedirs(bdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    backup = os.path.join(bdir, "%s.%s.bak" % (os.path.basename(path), stamp))
    n = 1
    while os.path.exists(backup):
        backup = os.path.join(bdir, "%s.%s-%d.bak" % (
            os.path.basename(path), stamp, n))
        n += 1
    shutil.copy2(path, backup)
    return backup


def _atomic_write(path, text):
    """Write exact bytes (no newline translation) via a tmp file + replace."""
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(text.encode("utf-8"))
    os.replace(tmp, path)


def _json_style(raw):
    """(has_bom, trailing_newline) so a rewrite keeps the file's own flavour."""
    return (raw[:3] == b"\xef\xbb\xbf", raw.endswith(b"\n"))


def _dump_json(data, has_bom, trailing_nl):
    body = json.dumps(data, indent=2, ensure_ascii=False)
    if trailing_nl:
        body += "\n"
    if has_bom:
        return "\ufeff" + body
    return body


def fix_base_url(path, provider_id, new_base):
    """Point a provider's baseUrl at the API root (client-approved only)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        has_bom, nl = _json_style(raw)
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as e:
        return {"ok": False, "error": "cannot read json: %s" % e}
    provs = data.get("providers")
    if not isinstance(provs, dict):
        return {"ok": False, "error": "no providers block"}
    obj = provs.get((provider_id or "").strip())
    if not isinstance(obj, dict):
        return {"ok": False, "error": "provider not found"}
    old = (obj.get("baseUrl") or "").strip()
    new_base = (new_base or "").strip()
    if not new_base or new_base == old:
        return {"ok": False, "error": "nothing to change"}
    obj["baseUrl"] = new_base
    try:
        backup = _backup_file(path)
        _atomic_write(path, _dump_json(data, has_bom, nl))
    except Exception as e:
        return {"ok": False, "error": "cannot write: %s" % e}
    return {"ok": True, "provider_id": provider_id, "old": old, "new": new_base,
            "backup": backup}


def remove_provider(path, provider_id):
    """Drop a whole duplicate provider entry (client-approved only)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        has_bom, nl = _json_style(raw)
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as e:
        return {"ok": False, "error": "cannot read json: %s" % e}
    provs = data.get("providers")
    if not isinstance(provs, dict):
        return {"ok": False, "error": "no providers block"}
    pid = (provider_id or "").strip()
    if pid not in provs:
        return {"ok": False, "error": "provider not found: %s" % pid}
    del provs[pid]
    try:
        backup = _backup_file(path)
        _atomic_write(path, _dump_json(data, has_bom, nl))
    except Exception as e:
        return {"ok": False, "error": "cannot write: %s" % e}
    return {"ok": True, "provider_id": pid, "backup": backup}


def _remove_json_list(path, container, provider_id, model):
    """providers.<pid>.models[] shape - qoder / openai-generic."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        has_bom, nl = _json_style(raw)
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as e:
        return {"ok": False, "error": "cannot read json: %s" % e}
    if not isinstance(data, dict):
        return {"ok": False, "error": "file is not a json object"}
    provs = data.get(container)
    if not isinstance(provs, dict):
        return {"ok": False, "error": "no %s block" % container}
    pid = (provider_id or "").strip()
    obj = provs.get(pid)
    if not isinstance(obj, dict):
        return {"ok": False, "error": "provider not found: %s" % (pid or "?")}
    models = obj.get("models")
    if not isinstance(models, list):
        return {"ok": False, "error": "provider has no models list"}
    keep = [m for m in models if _model_entry_id(m) != model]
    if len(keep) == len(models):
        if (obj.get("model") or "") == model:      # only the default matched
            obj["model"] = ""
            return _finish_json(path, data, has_bom, nl, model, len(models),
                                True, "")
        return {"ok": False, "error": "model not in this provider's list"}
    default_moved = False
    if (obj.get("model") or "") == model:
        survivors = [m for m in keep if _model_entry_id(m)]
        obj["model"] = _model_entry_id(survivors[0]) if survivors else ""
        default_moved = True
    obj["models"] = keep
    return _finish_json(path, data, has_bom, nl, model, len(keep), default_moved,
                        obj.get("model") or "")


def _finish_json(path, data, has_bom, nl, model, remaining, default_moved,
                 default=""):
    try:
        backup = _backup_file(path)
        _atomic_write(path, _dump_json(data, has_bom, nl))
    except Exception as e:
        return {"ok": False, "error": "cannot write: %s" % e}
    return {"ok": True, "removed": model, "remaining": remaining,
            "default_moved": default_moved, "default": default,
            "backup": backup}


def _remove_json_dict(path, container, provider_id, model):
    """provider.<pid>.models{} shape (dict keyed by name) - zcode."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        has_bom, nl = _json_style(raw)
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as e:
        return {"ok": False, "error": "cannot read json: %s" % e}
    if not isinstance(data, dict):
        return {"ok": False, "error": "file is not a json object"}
    provs = data.get(container)
    if not isinstance(provs, dict):
        return {"ok": False, "error": "no %s block" % container}
    pid = (provider_id or "").strip()
    obj = provs.get(pid)
    if not isinstance(obj, dict):
        return {"ok": False, "error": "provider not found: %s" % (pid or "?")}
    models = obj.get("models")
    if not isinstance(models, dict):
        return {"ok": False, "error": "provider has no models map"}
    # the key is the written id; remove the key whose id matches
    hit = None
    for key, mv in models.items():
        nm = mv.get("name") if isinstance(mv, dict) else None
        if key == model or nm == model:
            hit = key
            break
    if hit is None:
        return {"ok": False, "error": "model not in this provider"}
    del models[hit]
    return _finish_json(path, data, has_bom, nl, model, len(models), False)


def _remove_cline(path, provider_id, model):
    """providers.<pid>.settings.model - a single string, so clearing is it."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        has_bom, nl = _json_style(raw)
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as e:
        return {"ok": False, "error": "cannot read json: %s" % e}
    pid = (provider_id or "").strip()
    obj = (data.get("providers") or {}).get(pid) if isinstance(data, dict) else None
    s = obj.get("settings") if isinstance(obj, dict) else None
    if not isinstance(s, dict) or (s.get("model") or "") != model:
        return {"ok": False, "error": "model not set for this provider"}
    s["model"] = ""
    return _finish_json(path, data, has_bom, nl, model, 0, False)


def _remove_env(path, model):
    """MODEL / MODEL_FALLBACKS in a .env-style text file."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        has_bom, nl = _json_style(raw)
        text = raw.decode("utf-8-sig")
    except Exception as e:
        return {"ok": False, "error": "cannot read env: %s" % e}
    lines = text.splitlines()
    changed = False
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key == "MODEL" and val == model:
            lines[i] = "# " + line + "   # removed by ModelPulse"
            changed = True
        elif key == "MODEL_FALLBACKS" and val:
            try:
                arr = json.loads(val)
            except Exception:
                continue
            if not isinstance(arr, list):
                continue
            new = [it for it in arr
                   if (it.get("model") if isinstance(it, dict) else it) != model]
            if len(new) != len(arr):
                quoted = '"' + json.dumps(new, ensure_ascii=False) + '"'
                lines[i] = "%s=%s" % (k.rstrip(), quoted)
                changed = True
    if not changed:
        return {"ok": False, "error": "model not found in env file"}
    out = "\n".join(lines) + ("\n" if nl else "")
    try:
        backup = _backup_file(path)
        _atomic_write(path, ("\ufeff" if has_bom else "") + out)
    except Exception as e:
        return {"ok": False, "error": "cannot write env: %s" % e}
    return {"ok": True, "removed": model, "remaining": 0,
            "default_moved": False, "backup": backup}


def _remove_toml_line(path, model):
    """Codex TOML: comment out the top-level `model = "..."` line."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        has_bom, nl = _json_style(raw)
        text = raw.decode("utf-8-sig")
    except Exception as e:
        return {"ok": False, "error": "cannot read toml: %s" % e}
    pat = re.compile(r'^(\s*model\s*=\s*["\'])' + re.escape(model) + r'(["\']\s*)$')
    lines = text.splitlines()
    changed = False
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = "# " + line + "   # removed by ModelPulse"
            changed = True
    if not changed:
        return {"ok": False, "error": "no top-level model line matches"}
    out = "\n".join(lines) + ("\n" if nl else "")
    try:
        backup = _backup_file(path)
        _atomic_write(path, ("\ufeff" if has_bom else "") + out)
    except Exception as e:
        return {"ok": False, "error": "cannot write toml: %s" % e}
    return {"ok": True, "removed": model, "remaining": 0,
            "default_moved": False, "backup": backup}


_FORMAT_EDITORS = {
    "qoder": lambda p, pid, m: _remove_json_list(p, "providers", pid, m),
    "openai-generic": lambda p, pid, m: _remove_json_list(p, "providers", pid, m),
    "zcode": lambda p, pid, m: _remove_json_dict(p, "provider", pid, m),
    "cline": lambda p, pid, m: _remove_cline(p, pid, m),
    "codex-toml": lambda p, pid, m: _remove_toml_line(p, m),
    "env": lambda p, pid, m: _remove_env(p, m),
}


def remove_model_in_source(source, provider_id, model):
    """Delete one model id from whatever config the source points at.

    source is a discover() dict: {format, path, ...}. Read-only formats
    (registry/catalog) are refused. Returns {ok, removed, remaining,
    default_moved, backup, error}; nothing is written unless ok is True.
    """
    fmt = (source or {}).get("format") or ""
    path = (source or {}).get("path") or ""
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "source file not found"}
    editor = _FORMAT_EDITORS.get(fmt)
    if editor is None:
        return {"ok": False, "format": fmt,
                "error": "format '%s' is metadata only (not editable)" % fmt}
    res = editor(path, provider_id, model)
    res.setdefault("format", fmt)
    res.setdefault("path", path)
    return res


def remove_model(path, provider_id, model):
    """Back-compat shim: edit a JSON providers/members file (qoder shape)."""
    src = {"format": "qoder", "path": path or resolve_settings_path()}
    return remove_model_in_source(src, provider_id, model)

