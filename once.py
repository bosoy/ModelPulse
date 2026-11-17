#!/usr/bin/env python3
"""ModelPulse once: run a single probe pass, save an HTML report, open it.

The report is a standalone file on disk, so it stays open in the browser
after this window closes. Double-click once.cmd or run:
    python once.py
Optional:
    python once.py --filter wusrouter --timeout 30 --max-tokens 4096
    python once.py --no-open      (render the file, do not launch a browser)
Source is ASCII-only (Python 3.14 on Windows reads .py as cp1251).
All user-facing Russian text lives in ui/i18n.ru.json.
"""

import argparse
import json
import os
import sys
import time

import core
import report
import storage

APP_DIR = core.APP_DIR
TEXT_REPORT = os.path.join(APP_DIR, "data", "last-run.txt")
OUT = []
LANGS = ("ru", "en")


def load_i18n(lang="ru"):
    lang = lang if lang in LANGS else "ru"
    for cand in (lang, "ru"):
        try:
            with open(os.path.join(core.RES_DIR, "ui", "i18n.%s.json" % cand),
                      "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            continue
    return {}


def say(line=""):
    OUT.append(line)
    print(line)


def setup_console():
    """Force UTF-8 on the Windows console so Russian text is readable."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def _short(text):
    return "".join(c if 32 <= ord(c) else " " for c in str(text)).strip()[:52]


def _explain_empty(src, agent, args, T):
    """Say why nothing is probeable instead of a bare 'nothing found'."""
    print(T.get("app_name", "ModelPulse") + " - " + T.get("run_once", "single pass"))
    if src is None:
        print(T.get("no_source", "no source resolved"))
        return
    print("%s: %s" % (T.get("agent_word", "agent"), agent))
    print("%s: %s" % (T.get("source", "source"), src.get("path")))
    print("%s: %d   %s: %d"
          % (T.get("models_word", "models"), src.get("model_count") or 0,
             T.get("providers", "providers"), src.get("provider_count") or 0))
    print("")
    cat = src.get("category") or "agent"
    if args.filter and (src.get("model_count") or 0) > 0:
        print(T.get("empty_filter", "filter matched nothing") + " (filter=%r)" % args.filter)
        return
    if cat == "registry":
        print(T.get("empty_registry",
                    "this is a model registry (metadata), not a configured agent - "
                    "nothing to probe here"))
    elif not src.get("has_keys"):
        print(T.get("empty_nokeys",
                    "this source has no API key, so it cannot be probed"))
    elif not src.get("probeable"):
        print(T.get("empty_notprobeable",
                    "this source is not OpenAI-compatible (e.g. anthropic/responses "
                    "format) - ModelPulse can only list its models"))
    else:
        print(T.get("empty_filter", "filter matched nothing") + " (filter=%r)" % args.filter)
    print("")
    print(T.get("empty_hint",
                "pick a probeable agent: python once.py --list-sources")
          + "   |   " + T.get("empty_hint2", "or set active_source in the dashboard"))


def console_table(results, s, elapsed, T):
    say("%-6s %-34s %-26s %7s %5s  %s"
        % (T.get("col_st", "st"), T.get("col_model", "model"),
           T.get("col_host", "host"), T.get("col_ms", "ms"),
           T.get("col_http", "http"), T.get("col_note", "note")))
    say("-" * 116)
    for r in results:
        st = {"reply": T.get("st_reply", "REPLY"),
              "empty": T.get("st_empty", "EMPTY"),
              "fail": T.get("st_fail", "FAIL")}.get(r["status"], "?")
        note = r["reply"] if r["status"] == "reply" else (r["error"] or "")
        say("%-6s %-34s %-26s %7d %5s  %s" % (
            st, r["model"][:34], r["host"][:26], r["ms"],
            r["http_code"] if r["http_code"] else "-", _short(note)))
    say("-" * 116)
    say(T.get("totals", "total %d | replied %d | empty %d | failed %d")
        % (s["total"], s["replied"], s["empty"], s["failed"])
        + " | %.1f%s" % (elapsed, T.get("sec_short", "s")))


def main():
    setup_console()
    # language: explicit flag > config.json > Russian
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang", default="")
    known, _ = pre.parse_known_args()
    lang = (known.lang or "").strip().lower()
    if lang not in LANGS:
        try:
            lang = (core.load_config().get("language") or "ru").strip().lower()
        except Exception:
            lang = "ru"
    if lang not in LANGS:
        lang = "ru"
    T = load_i18n(lang)

    ap = argparse.ArgumentParser(description="ModelPulse single pass")
    ap.add_argument("--filter", default="", help="substring on model / provider / host")
    ap.add_argument("--timeout", type=float, default=None)
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--lang", default="", help="ru or en (default: from config.json)")
    ap.add_argument("--source", default="", help="agent source id (see --list-sources)")
    ap.add_argument("--mode", default="", choices=["", "quick", "full"],
                    help="quick = one-word ping (instant, less reliable); "
                         "full = chat-sized answer (slower, honest); "
                         "default: from config.json")
    ap.add_argument("--deep", action="store_true", help="deep scan when listing sources")
    ap.add_argument("--list-sources", action="store_true",
                    help="print discovered agents and exit")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--no-open", action="store_true",
                    help="do not launch a browser window")
    args = ap.parse_args()

    if args.list_sources:
        import discover
        found = discover.discover(deep=args.deep)
        print(discover.summary_line(found))
        for s in found:
            print("  %-16s %-22s models=%-5d probe=%-5s keys=%-5s %s"
                  % ("[" + s["format"] + "]", s["agent"], s["model_count"],
                     s["probeable"], s["has_keys"], s["path"]))
        return 0

    cfg = core.load_config()
    timeout = args.timeout if args.timeout is not None else float(cfg.get("timeout_seconds") or 30)
    max_tokens = args.max_tokens if args.max_tokens is not None else int(cfg.get("max_tokens") or core.DEFAULT_MAX_TOKENS)
    workers = args.workers if args.workers is not None else int(cfg.get("workers") or 8)
    scan_mode = args.mode or (cfg.get("scan_mode") or "full")
    if scan_mode not in core.SCAN_MODES:
        scan_mode = "full"

    try:
        src, path, mtime, targets = core.load_targets_for(args.source or None,
                                                          deep=args.deep)
    except Exception as e:
        print("cannot read settings: %s" % e)
        return 2
    agent = src["agent"] if src else "Qoder"
    if args.source and src and src["id"] != args.source:
        print("%s %r -> %s" % (T.get("unknown_source", "unknown source, using"),
                               args.source, src["id"]))

    if args.filter:
        f = args.filter.lower()
        targets = [t for t in targets
                   if f in t["model"].lower() or f in t["provider_id"].lower()
                   or f in t["host"].lower()]

    if not targets:
        _explain_empty(src, agent, args, T)
        return 1

    say("%s - %s   %s" % (T.get("app_name", "ModelPulse"),
                          T.get("run_once", "single pass"),
                          time.strftime("%Y-%m-%d %H:%M:%S")))
    say("%s: %s" % (T.get("agent_word", "agent"), agent))
    say("%s: %s" % (T.get("source", "source"), path))
    say("%s: %d   %s: %gs   %s: %d   %s: %s"
        % (T.get("models_word", "models"), len(targets),
           T.get("timeout_word", "timeout"), timeout,
           T.get("max_tokens", "max_tokens"), max_tokens,
           T.get("mode_word", "mode"), scan_mode))
    say(T.get("probing", "probing..."))
    say("")

    # respect mutes: skip models the user wrote off, and carry the dead verdict
    # from stored history so the report can single them out
    hidden_rows = []
    try:
        store = storage.Store()
        muted = store.hidden_set()
        hidden_rows = store.hidden_list()
    except Exception:
        store, muted = None, set()
    if muted:
        skipped = [t for t in targets if (t["model"], t["host"]) in muted]
        targets = [t for t in targets if (t["model"], t["host"]) not in muted]
        if skipped:
            say("%s: %d" % (T.get("skipped_hidden", "skipped (hidden)"), len(skipped)))

    t0 = time.time()
    results = core.scan(targets, timeout=timeout, workers=workers,
                        max_tokens=max_tokens, mode=scan_mode, lang=lang)
    elapsed = time.time() - t0

    # enrich with the dead verdict from history (dashboard-parity)
    if store is not None:
        verdicts = {(r["model"], r["host"]): r for r in store.latest_per_model()}
        for r in results:
            v = verdicts.get((r["model"], r["host"]))
            if v:
                r["dead"] = v["dead"]
                r["reason_code"] = v["reason_code"]
                r["reason_text"] = v["reason_text"]

    rank = {"reply": 0, "empty": 1, "fail": 2}
    results.sort(key=lambda r: (rank.get(r["status"], 3), r["host"], r["model"]))
    s = core.summarize(results)

    console_table(results, s, elapsed, T)
    say(T.get("legend", ""))

    if not args.no_save:
        try:
            os.makedirs(os.path.dirname(TEXT_REPORT), exist_ok=True)
            with open(TEXT_REPORT, "w", encoding="utf-8") as fh:
                fh.write("\n".join(OUT) + "\n")
        except Exception as e:
            print("could not save text report: %s" % e)

        meta = {
            "ts": time.time(),
            "source": path,
            "agent": agent,
            "lang": lang,
            "nmodels": len(targets),
            "nproviders": len({t["provider_id"] for t in targets}),
            "timeout": timeout,
            "max_tokens": max_tokens,
            "workers": workers,
            "elapsed": elapsed,
            "hidden": [{"model": h["model"], "host": h["host"],
                        "reason_code": h.get("reason_code") or "",
                        "reason_text": h.get("reason_text") or ""}
                       for h in hidden_rows],
        }
        try:
            page, _ = report.write_report(results, meta, T)
            print("")
            print("%s: %s" % (T.get("report_saved", "report saved"), page))
            if not args.no_open:
                ok, how = report.open_report(page)
                if ok:
                    print("%s: %s" % (T.get("report_opened", "opened in browser"), how))
                else:
                    print("%s: %s" % (T.get("report_open_failed", "could not open browser"), how))
        except Exception as e:
            print("could not write html report: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
