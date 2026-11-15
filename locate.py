#!/usr/bin/env python3
"""ModelPulse locate: read-only search for where a model id is written.

The point is trust: we never guess and never edit on our own. For every
discovered source we point at the exact place a model id lives (file, key
path, list index), so the client can fix it by hand - or explicitly approve
a targeted edit. This module only reads.

Source is ASCII-only (Python 3.14 on Windows parses .py as cp1251).
"""

import json

import core

# formats we can write back to (used by the edit layer, not by search)
EDITABLE_FORMATS = ("qoder", "openai-generic", "zcode", "cline", "env", "codex-toml")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return None


def _hit(src, pid, model, how, editable, role="list", alias=""):
    return {
        "source_id": src.get("id") or "",
        "agent": src.get("agent") or "",
        "path": src.get("path") or "",
        "format": src.get("format") or "",
        "provider_id": pid or "",
        "model": model or "",
        "how": how,
        "editable": bool(editable),
        "role": role,
        "alias": alias or "",
    }


def _list_models(obj, container, pid_path, src, editable):
    """providers.<pid>.models[] shape (list of entries) - qoder / generic."""
    out = []
    for pid, o in (obj.get(container) or {}).items():
        if not isinstance(o, dict):
            continue
        models = o.get("models")
        if isinstance(models, list):
            for i, m in enumerate(models):
                mid = core._model_entry_id(m)
                if mid:
                    out.append(_hit(src, pid, mid,
                                    "%s.%s.models[%d]" % (pid_path, pid, i),
                                    editable, "list"))
        if o.get("model"):
            out.append(_hit(src, pid, o["model"], "%s.%s.model" % (pid_path, pid),
                            editable, "default"))
    return out


def _dict_models(obj, container, pid_path, src, editable):
    """provider.<pid>.models{} shape (dict keyed by name) - zcode.

    The dict key is the written id; a nested `name` is only a display alias,
    so it is carried as `alias`, not as a second hit.
    """
    out = []
    for pid, o in (obj.get(container) or {}).items():
        if not isinstance(o, dict):
            continue
        models = o.get("models")
        if isinstance(models, dict):
            for key, mv in models.items():
                alias = mv.get("name") if isinstance(mv, dict) else None
                out.append(_hit(src, pid, key,
                                "%s.%s.models.%s" % (pid_path, pid, key),
                                editable, "list", alias if alias != key else ""))
    return out


def _env_models(text, src, editable):
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k == "MODEL" and v:
            out.append(_hit(src, "env", v, "MODEL", editable, "default"))
        elif k == "MODEL_FALLBACKS" and v:
            try:
                arr = json.loads(v)
            except Exception:
                continue
            for i, item in enumerate(arr if isinstance(arr, list) else []):
                mid = item.get("model") if isinstance(item, dict) else item
                if mid:
                    out.append(_hit(src, "env", mid,
                                    "MODEL_FALLBACKS[%d]" % i, editable, "list"))
    return out


def locations_for_source(src):
    """Every model id written in one source, each with its in-file address."""
    fmt = src.get("format") or ""
    path = src.get("path") or ""
    editable = fmt in EDITABLE_FORMATS
    if fmt in ("qoder", "openai-generic"):
        obj = _read_json(path)
        return _list_models(obj, "providers", "providers", src, editable) \
            if isinstance(obj, dict) else []
    if fmt == "zcode":
        obj = _read_json(path)
        return _dict_models(obj, "provider", "provider", src, editable) \
            if isinstance(obj, dict) else []
    if fmt == "cline":
        obj = _read_json(path)
        if not isinstance(obj, dict):
            return []
        out = []
        for pid, o in (obj.get("providers") or {}).items():
            s = o.get("settings") if isinstance(o, dict) else None
            if isinstance(s, dict) and s.get("model"):
                out.append(_hit(src, pid, s["model"],
                                "providers.%s.settings.model" % pid, editable,
                                "default"))
        return out
    if fmt == "codex-toml":
        try:
            import tomllib
            with open(path, "rb") as fh:
                obj = tomllib.load(fh)
        except Exception:
            return []
        out = []
        if obj.get("model"):
            out.append(_hit(src, "active", obj["model"], "model", editable,
                            "default"))
        if obj.get("model_provider"):
            out.append(_hit(src, obj["model_provider"], obj["model_provider"],
                            "model_provider", editable, "default"))
        return out
    if fmt == "env":
        return _env_models(_read_text(path), src, editable)
    # registry / catalog / mcp-only: list from the inventory, no in-file address
    out = []
    for p in src.get("providers") or []:
        for mid in p.get("models") or []:
            out.append(_hit(src, p.get("id"), mid, "", False, "list"))
    return out


def find_model(query, deep=False, include_meta=False):
    """Locate a model id across every discovered source (read-only).

    Registry/catalog sources are pure metadata (never the client's config),
    so they are skipped unless include_meta is set - otherwise a search for a
    common name drowns in thousands of catalog rows.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    hits = []
    for src in core.list_sources(deep=deep):
        if not include_meta and src.get("category") in ("registry", "catalog"):
            continue
        for h in locations_for_source(src):
            if q in (h["model"] or "").lower():
                hits.append(h)
    hits.sort(key=lambda h: (not h["editable"], h["agent"], h["path"], h["how"]))
    return hits


if __name__ == "__main__":
    import sys
    for row in find_model(" ".join(sys.argv[1:]) or ""):
        print("%-22s %-9s %s" % (row["agent"], "editable" if row["editable"] else "-",
                                 row["how"]))
        print("    %s  [%s]" % (row["model"], row["path"]))
