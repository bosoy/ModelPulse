#!/usr/bin/env python3
"""ModelPulse discover: find AI agents on this machine and read their model lists.

Two layers:
  * a fixed catalog of known agent config paths (fast, deterministic);
  * an optional deep walk of the user profile that sniffs unknown JSON/TOML
    files for a providers/models shape.

Safety rules, always on:
  * API key VALUES are never stored, logged or emitted - only a has_key flag
    and the key length. Inventory output carries no secrets.
  * the walk is bounded: depth, file count, per-file size, and a pruned set of
    cache/system directories. No full-disk crawl.

Source is ASCII-only (Python 3.14 on Windows parses .py as cp1251).
"""

import json
import os
import re
import time

HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA") or ""
LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or ""

MAX_FILES = 40000
MAX_PARSE_BYTES = 12 * 1024 * 1024
MAX_CANDIDATES = 400
WALK_DEPTH = 5
REGISTRY_MAX_PROVIDERS = 80

# directories that only ever hold caches, deps or binaries: never scan
PRUNE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "site-packages", "dist-info", "dist", "build", ".gradle", ".nuget",
    ".m2", ".cargo", ".rustup", ".dotnet", ".android", ".emulator",
    "cache", "caches", "gpucache", "gpu cache", "code cache", "dawncache",
    "dawnwebgpucache", "dawngraphitecache", "blob_storage", "service worker",
    "crashpad", "logs", "log", "tmp", "temp", "downloads", "download-jobs",
    "dips", "sharedstorage", "local storage", "indexeddb", "shadercache",
    "grshadercache", "component_crx_cache", "extensions_crx_cache",
    "*.framework", "wheels", "pip", "npm", "npm-cache", "uv", "poetry",
}

SNIFF_EXT = (".json", ".jsonc", ".toml", ".yaml", ".yml", ".conf", ".cfg", ".ini")
SNIFF_NAME = ("config", "settings", "provider", "model", "catalog", "agents", "mcp")

# (agent_key, label, path, format) - tilde-prefixed path templates
CATALOG = [
    ("qoder",          "Qoder",            "~/.qoder/settings.json",                        "qoder"),
    ("zcode",          "zcode",            "~/.zcode/v2/config.json",                       "zcode"),
    ("zcode-cache",    "zcode",            "~/.zcode/v2/bots-model-cache.v2.json",          "zcode-cache"),
    ("codex",          "Codex CLI",        "~/.codex/config.toml",                          "codex-toml"),
    ("codex-catalog",  "Codex CLI",        "~/.codex/ccr-model-catalog.json",               "codex-catalog"),
    ("fcc-catalog",    "free-claude-code", "~/.fcc/codex-model-catalog.json",               "codex-catalog"),
    ("fcc-env",        "free-claude-code", "~/.fcc/.env",                                   "env"),
    ("cline",          "Cline",            "~/.cline/data/settings/providers.json",         "cline"),
    ("claude",         "Claude Code",      "~/.claude.json",                                "claude"),
    ("gemini",         "Gemini CLI",       "~/.gemini/settings.json",                       "mcp-only"),
    ("copilot",        "Copilot CLI",      "~/.copilot",                                    "copilot"),
    ("lmstudio",       "LM Studio",        "~/.lmstudio",                                   "lmstudio-dir"),
    ("ollama",         "Ollama",           "~/.ollama/config.json",                         "ollama"),
    ("continue",       "Continue",         "~/.continue/config.json",                       "continue"),
    ("aider",          "Aider",            "~/.aider.conf.yml",                             "aider"),
    ("kilo-registry",  "kilo (registry)",  "~/.cache/kilo/models.json",                     "registry"),
    ("opencode-reg",   "opencode (registry)", "~/.cache/opencode/models.json",               "registry"),
]

_KEY_HINT = re.compile(r"(api[_-]?key|apikey|token|secret|bearer|password)", re.I)


def _expand(p):
    return os.path.expanduser(p) if p else p


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except Exception:
        return None


def _read_text(path, limit=MAX_PARSE_BYTES):
    try:
        if os.path.getsize(path) > limit:
            return None
    except Exception:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return None


def _parse_structured(path):
    """Best-effort parse of json/jsonc/toml/yaml into a python object."""
    text = _read_text(path)
    if text is None:
        return None
    low = path.lower()
    if low.endswith((".json", ".jsonc")):
        try:
            return json.loads(text)
        except Exception:
            # tolerate // comments if present
            stripped = re.sub(r"^\s*//.*$", "", text, flags=re.M)
            try:
                return json.loads(stripped)
            except Exception:
                return None
    if low.endswith(".toml"):
        try:
            import tomllib
            return tomllib.loads(text)
        except Exception:
            return None
    if low.endswith((".yaml", ".yml")):
        return _mini_yaml(text)
    return None


def _mini_yaml(text):
    """Very small top-level-only yaml reader (key: value and - item lists)."""
    out = {}
    try:
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[0] in " \t":
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
    except Exception:
        return None
    return out or None


def _key_bits(obj):
    """Return (has_key, key_len) without ever revealing the value."""
    if not isinstance(obj, str):
        return False, 0
    return (bool(obj.strip()), len(obj))


def _providers_from_openai(provs):
    """providers = {id: {baseUrl/base_url, apiKey, models:[...] | model}}."""
    out = []
    for pid, obj in (provs or {}).items():
        if not isinstance(obj, dict):
            continue
        base = (obj.get("baseUrl") or obj.get("base_url") or obj.get("baseURL")
                or obj.get("endpoint") or "").strip()
        has_key, klen = _key_bits(obj.get("apiKey") or obj.get("api_key")
                                  or obj.get("key"))
        models = []
        for m in obj.get("models") or []:
            if isinstance(m, dict):
                mid = m.get("model") or m.get("id") or m.get("name")
            else:
                mid = m
            if mid:
                models.append(str(mid))
        if not models and obj.get("model"):
            models.append(str(obj["model"]))
        if not base and not models:
            continue
        out.append({"id": pid, "base": base, "models": models,
                    "has_key": has_key, "key_len": klen,
                    "enabled": obj.get("enabled")})
    return out


def _providers_from_zcode(provs):
    out = []
    for pid, obj in (provs or {}).items():
        if not isinstance(obj, dict):
            continue
        opts = obj.get("options") or {}
        base = (opts.get("baseURL") or opts.get("baseUrl") or "").strip()
        has_key, klen = _key_bits(opts.get("apiKey") or opts.get("api_key"))
        models = []
        if isinstance(obj.get("models"), dict):
            for mid, m in obj["models"].items():
                name = (m.get("name") if isinstance(m, dict) else None) or mid
                models.append(str(name))
        elif isinstance(obj.get("models"), list):
            for m in obj["models"]:
                if isinstance(m, dict):
                    models.append(str(m.get("id") or m.get("name") or ""))
                elif m:
                    models.append(str(m))
        models = [m for m in models if m]
        out.append({"id": pid, "base": base, "models": models, "has_key": has_key,
                    "key_len": klen, "kind": obj.get("kind"),
                    "enabled": obj.get("enabled")})
    return out


def _providers_from_zcode_cache(provs):
    out = []
    for obj in (provs or []):
        if not isinstance(obj, dict):
            continue
        ep = obj.get("endpoints") or {}
        base = (ep.get("baseURL") or "").strip()
        has_key, klen = _key_bits(obj.get("apiKey"))
        models = []
        for m in obj.get("models") or []:
            if isinstance(m, dict):
                models.append(str(m.get("id") or m.get("name") or ""))
            elif m:
                models.append(str(m))
        models = [m for m in models if m]
        out.append({"id": obj.get("id") or "?", "base": base, "models": models,
                    "has_key": has_key, "key_len": klen,
                    "kind": obj.get("apiFormat"), "enabled": obj.get("enabled")})
    return out


def _providers_from_catalog(obj):
    models = []
    if isinstance(obj, dict) and isinstance(obj.get("models"), list):
        for m in obj["models"]:
            if isinstance(m, dict):
                mid = m.get("slug") or m.get("id") or m.get("display_name")
            else:
                mid = m
            if mid:
                models.append(str(mid))
    return [{"id": "catalog", "base": "", "models": models,
             "has_key": False, "key_len": 0, "enabled": None}]


def _providers_from_codex_toml(obj):
    out = []
    provs = (obj or {}).get("model_providers") or {}
    for pid, o in provs.items():
        if not isinstance(o, dict):
            continue
        base = (o.get("base_url") or "").strip()
        wire = o.get("wire_api") or ""
        has_key, klen = _key_bits(o.get("experimental_bearer_token"))
        out.append({"id": pid, "base": base, "models": [],
                    "has_key": has_key, "key_len": klen,
                    "kind": wire, "enabled": None})
    top_model = (obj or {}).get("model")
    if top_model:
        out.append({"id": "active", "base": "", "models": [str(top_model)],
                    "has_key": False, "key_len": 0, "kind": "active-model"})
    return out


def _providers_from_cline(obj):
    out = []
    for pid, o in ((obj or {}).get("providers") or {}).items():
        settings = (o or {}).get("settings") or {}
        models = []
        if settings.get("model"):
            models.append(str(settings["model"]))
        out.append({"id": pid, "base": "", "models": models,
                    "has_key": bool(o.get("tokenSource")), "key_len": 0,
                    "kind": settings.get("provider"), "enabled": None})
    return out


def _looks_like_registry(path_peek):
    """models.dev-style registry: {provider: {id, env, api, models:{...}}}."""
    if not isinstance(path_peek, dict):
        return False
    for _pid, o in list(path_peek.items())[:5]:
        if not isinstance(o, dict):
            continue
        if isinstance(o.get("models"), dict) and ("env" in o or "npm" in o or "doc" in o):
            return True
    return False


def _providers_from_registry(obj):
    """Registry metadata (ids only). Not a user config: no keys, not probeable.

    Used for kilo / opencode / models.dev caches so the model list is visible
    without pretending there is an endpoint to call.
    """
    out = []
    for pid, o in list((obj or {}).items())[:REGISTRY_MAX_PROVIDERS]:
        if not isinstance(o, dict):
            continue
        models = []
        for mid in list((o.get("models") or {}).keys())[:4000]:
            models.append(str(mid))
        out.append({"id": pid, "base": "", "models": models,
                    "has_key": False, "key_len": 0,
                    "kind": "registry", "enabled": None})
    return out


def _providers_from_env(text):
    """Report only the MODEL id and whether a key var exists - never its value."""
    model = ""
    has_key = False
    klen = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k.upper() in ("MODEL", "MODEL_NAME", "DEFAULT_MODEL") and v:
            model = v
        elif _KEY_HINT.search(k) and v:
            has_key = True
            klen = max(klen, len(v))
    models = [model] if model else []
    base = ""
    return [{"id": "env", "base": base, "models": models, "has_key": has_key,
             "key_len": klen, "kind": "env", "enabled": None}]


def _lmstudio_models(root):
    models = []
    mdir = os.path.join(root, "models")
    for base in (mdir, root):
        if not os.path.isdir(base):
            continue
        for dirpath, dirs, files in os.walk(base):
            if dirpath.count(os.sep) - base.count(os.sep) >= 3:
                dirs[:] = []
            for f in files:
                if f.lower().endswith(".gguf"):
                    models.append(os.path.splitext(f)[0])
        if models:
            break
    return models[:200]


def parse_source(agent_key, label, path, fmt):
    """Return a source dict for one config path, or None if unusable."""
    if not path or not os.path.exists(path):
        return None
    src = {
        "id": agent_key,
        "agent": label,
        "path": os.path.abspath(path),
        "format": fmt,
        "mtime": _mtime(path),
        "is_dir": os.path.isdir(path),
        "providers": [],
        "models": [],
        "model_count": 0,
        "provider_count": 0,
        "has_keys": False,
        "probeable": False,
        "note": "",
    }
    try:
        if fmt == "lmstudio-dir":
            models = _lmstudio_models(path)
            src["models"] = models
            src["model_count"] = len(models)
            src["note"] = "local gguf models"
            return src if models else None

        if fmt == "mcp-only":
            obj = _parse_structured(path) or {}
            n = len((obj.get("mcpServers") or {}))
            src["note"] = "mcpServers only (%d) - no model list" % n
            return src if n else None

        if fmt == "copilot":
            if not os.path.isdir(path):
                return None
            src["note"] = "session state only - no model list"
            return src

        if fmt == "env":
            text = _read_text(path)
            provs = _providers_from_env(text)
            src["providers"] = provs
            src["models"] = [m for p in provs for m in p["models"]]
            src["has_keys"] = any(p["has_key"] for p in provs)
            src["note"] = "env file - key value never read"
            src["model_count"] = len(src["models"])
            src["provider_count"] = len(provs)
            return src if (src["models"] or src["has_keys"]) else None

        if fmt == "registry":
            text = _read_text(path, limit=MAX_PARSE_BYTES)
            if text is None:
                return None
            try:
                obj = json.loads(text)
            except Exception:
                return None
            provs = _providers_from_registry(obj)
            src["providers"] = provs
            src["models"] = [m for p in provs for m in p["models"]]
            src["model_count"] = len(src["models"])
            src["provider_count"] = len(provs)
            src["note"] = "model registry (metadata) - not your config, not probeable"
            return src if src["models"] else None

        obj = _parse_structured(path)
        if obj is None:
            return None

        if fmt == "qoder":
            provs = _providers_from_openai(obj.get("providers"))
        elif fmt == "zcode":
            provs = _providers_from_zcode(obj.get("provider"))
        elif fmt == "zcode-cache":
            provs = _providers_from_zcode_cache(obj.get("providers"))
        elif fmt == "codex-catalog":
            provs = _providers_from_catalog(obj)
        elif fmt == "codex-toml":
            provs = _providers_from_codex_toml(obj)
        elif fmt == "cline":
            provs = _providers_from_cline(obj)
        else:
            provs = []

        src["providers"] = provs
        src["models"] = [m for p in provs for m in p["models"]]
        src["has_keys"] = any(p["has_key"] for p in provs)
        src["model_count"] = len(src["models"])
        src["provider_count"] = len(provs)
        src["probeable"] = _is_probeable(provs)
        if not src["models"] and not provs:
            return None
        return src
    except Exception as e:
        src["note"] = "parse error: " + str(e)[:80]
        return src


def _is_probeable(provs):
    """True if we can build OpenAI-compatible chat targets (base + key)."""
    for p in provs:
        if p.get("base") and p.get("has_key") and p.get("models"):
            kind = (p.get("kind") or "").lower()
            if kind in ("responses", "anthropic", "anthropic-messages",
                        "active-model", "env"):
                continue
            return True
    return False


def _sniff(obj):
    """Classify an unknown parsed file; return (format, providers) or None."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("providers"), dict):
        provs = _providers_from_openai(obj["providers"])
        if any(p["base"] or p["models"] for p in provs):
            return "openai-generic", provs
    if isinstance(obj.get("provider"), dict):
        provs = _providers_from_zcode(obj["provider"])
        if any(p["models"] for p in provs):
            return "zcode-generic", provs
    if isinstance(obj.get("models"), list) and obj["models"]:
        first = obj["models"][0]
        if isinstance(first, dict) and ("slug" in first or "id" in first):
            provs = _providers_from_catalog(obj)
            if provs[0]["models"]:
                return "catalog-generic", provs
    if _looks_like_registry(obj):
        provs = _providers_from_registry(obj)
        if any(p["models"] for p in provs):
            return "registry-generic", provs
    return None


def _in_prune(name):
    low = name.lower()
    return low in PRUNE_DIRS or low.endswith(".framework")


def walk_candidates(roots):
    """Yield candidate config file paths under roots, bounded and pruned."""
    seen = 0
    out = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        base_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirs, files in os.walk(root):
            if dirpath.count(os.sep) - base_depth >= WALK_DEPTH:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not _in_prune(d)]
            for f in files:
                seen += 1
                if seen > MAX_FILES:
                    return out
                low = f.lower()
                if low.endswith(".env") or low == ".env":
                    out.append(os.path.join(dirpath, f))
                    if len(out) >= MAX_CANDIDATES:
                        return out
                    continue
                if not low.endswith(SNIFF_EXT):
                    continue
                if not any(w in low for w in SNIFF_NAME):
                    continue
                out.append(os.path.join(dirpath, f))
                if len(out) >= MAX_CANDIDATES:
                    return out
    return out


def discover(deep=False, roots=None):
    """Return the list of discovered sources, newest-known first.

    deep=True additionally walks the profile and sniffs unknown files.
    """
    known_paths = set()
    sources = []
    for agent_key, label, tmpl, fmt in CATALOG:
        path = _expand(tmpl)
        known_paths.add(os.path.normcase(os.path.abspath(path)))
        src = parse_source(agent_key, label, path, fmt)
        if src:
            sources.append(src)

    if deep:
        if roots is None:
            roots = [HOME, APPDATA, LOCALAPPDATA]
        for cand in walk_candidates(roots):
            key = os.path.normcase(os.path.abspath(cand))
            if key in known_paths:
                continue
            if os.path.basename(cand).lower() in (".env", ".env.local"):
                text = _read_text(cand)
                if text and any(_KEY_HINT.search(l.split("=", 1)[0])
                                for l in text.splitlines() if "=" in l):
                    provs = _providers_from_env(text)
                    if provs[0]["models"] or provs[0]["has_key"]:
                        sources.append(_wrap("deep-env:" + key, "env", cand,
                                             "env", provs))
                continue
            obj = _parse_structured(cand)
            sniffed = _sniff(obj)
            if not sniffed:
                continue
            fmt, provs = sniffed
            sources.append(_wrap("deep:" + key, "unknown", cand, fmt, provs))

    for s in sources:
        s["probeable"] = _is_probeable(s["providers"])
        s["category"] = _category(s)
    sources.sort(key=lambda s: (_cat_rank(s["category"]), not s["probeable"],
                                -(s["model_count"] or 0), s["agent"]))
    return sources


_CAT_RANK = {"agent": 0, "catalog": 1, "registry": 2}


def _cat_rank(cat):
    return _CAT_RANK.get(cat, 3)


def _category(src):
    """agent = a real configured client; catalog/registry = static metadata."""
    fmt = src.get("format") or ""
    if "registry" in fmt:
        return "registry"
    if fmt in ("codex-catalog", "zcode-cache"):
        return "catalog"
    return "agent"


def _wrap(sid, label, path, fmt, provs):
    models = [m for p in provs for m in p["models"]]
    return {
        "id": sid, "agent": label, "path": os.path.abspath(path), "format": fmt,
        "mtime": _mtime(path), "is_dir": os.path.isdir(path),
        "providers": provs, "models": models, "model_count": len(models),
        "provider_count": len(provs),
        "has_keys": any(p["has_key"] for p in provs),
        "probeable": _is_probeable(provs),
        "note": "found by deep scan",
    }


def to_targets(source):
    """Build probe targets (OpenAI chat) from a source, or [] if not probeable."""
    targets = []
    seen = set()
    for p in source.get("providers") or []:
        base = (p.get("base") or "").strip()
        kind = (p.get("kind") or "").lower()
        if not base or kind in ("responses", "anthropic", "anthropic-messages",
                                "active-model", "env", "registry", "lmstudio",
                                "sapaicore"):
            continue
        if not p.get("has_key"):
            continue
        root = base.rstrip("/")
        for suf in ("/chat/completions", "/models"):
            if root.endswith(suf):
                root = root[: -len(suf)]
        root = root.rstrip("/")
        host = root.split("//", 1)[1].split("/", 1)[0] if "//" in root else root
        for mid in p.get("models") or []:
            sig = (root, mid)
            if sig in seen:
                continue
            seen.add(sig)
            targets.append({"provider_id": p["id"], "root": root, "host": host,
                            "model": mid, "key_ref": source["id"]})
    return targets


def keys_for(source):
    """Re-read the source file and return {provider_id: api_key} in memory only.

    Values are never cached, logged or returned to the UI - this exists solely
    so the probe can authenticate. Any provider not found maps to "".
    """
    fmt = source.get("format")
    path = source.get("path")
    keys = {}
    if not path or not os.path.isfile(path):
        return keys

    if fmt == "env":
        for line in (_read_text(path) or "").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                if _KEY_HINT.search(k) and "MODEL" not in k.upper():
                    keys["env"] = v.strip().strip('"').strip("'")
        return keys

    obj = _parse_structured(path)
    if not isinstance(obj, dict):
        return keys
    try:
        if fmt == "qoder":
            for pid, o in (obj.get("providers") or {}).items():
                if isinstance(o, dict):
                    keys[pid] = (o.get("apiKey") or "").strip()
        elif fmt == "zcode":
            for pid, o in (obj.get("provider") or {}).items():
                opts = (o or {}).get("options") or {}
                keys[pid] = (opts.get("apiKey") or "").strip()
        elif fmt in ("openai-generic",):
            for pid, o in (obj.get("providers") or {}).items():
                keys[pid] = (o.get("apiKey") or o.get("api_key") or "").strip()
        elif fmt in ("zcode-generic",):
            for pid, o in (obj.get("provider") or {}).items():
                opts = (o or {}).get("options") or {}
                keys[pid] = (opts.get("apiKey") or "").strip()
    except Exception:
        pass
    return keys


def targets_for_source(source):
    """Targets with real in-memory keys attached, ready for core.scan."""
    keys = keys_for(source)
    out = []
    for t in to_targets(source):
        t = dict(t)
        t["key"] = keys.get(t["provider_id"], "")
        if not t["key"]:
            continue
        out.append(t)
    return out


def summary_line(sources):
    probe = sum(1 for s in sources if s.get("probeable"))
    return "agents=%d probeable=%d models=%d" % (
        len(sources), probe, sum(s.get("model_count") or 0 for s in sources))


if __name__ == "__main__":
    import sys
    deep = "--deep" in sys.argv
    found = discover(deep=deep)
    print(summary_line(found))
    for s in found:
        print("  %-16s %-22s models=%-4d probe=%-5s %s"
              % ("[" + s["format"] + "]", s["agent"], s["model_count"],
                 s["probeable"], s["path"]))
