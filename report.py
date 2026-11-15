#!/usr/bin/env python3
"""ModelPulse report: render a standalone HTML page for one probe pass.

Self-contained (inline CSS, no server, no JS). Written to disk so it never
disappears when the console window closes, then opened in the browser.
Source is ASCII-only (Python 3.14 on Windows reads .py as cp1251).
All user-facing Russian text comes from ui/i18n.ru.json.
"""

import html
import json
import os
import subprocess
import time
from pathlib import Path

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
REPORT_DIR = os.path.join(APP_DIR, "data", "reports")
KEEP_REPORTS = 50
LANGS = ("ru", "en")


def _pack_path(lang):
    return os.path.join(APP_DIR, "ui", "i18n.%s.json" % lang)

_CSS = """
:root{--bg:#0e1116;--panel:#151a22;--panel2:#1b222c;--line:#263040;--fg:#d7dee8;
--dim:#7d8ba0;--up:#35c46a;--empty:#d8a531;--fail:#e0554e;--accent:#4c9aff;}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg);
font:13px/1.5 "Segoe UI",Roboto,system-ui,sans-serif}
header{padding:14px 20px 10px;border-bottom:1px solid var(--line);background:var(--panel)}
.brand{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
h1{font-size:16px;margin:0;letter-spacing:.4px}
.tag{color:var(--dim);font-size:12px}
.when{margin-left:auto;color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}
.meta{padding:8px 20px;color:var(--dim);font-size:12px;display:flex;gap:16px;flex-wrap:wrap}
.meta b{color:var(--fg);font-weight:500}
.meta .p{font-family:Consolas,"Cascadia Mono",monospace;word-break:break-all}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;padding:12px 20px 4px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.card .v{font-size:24px;font-weight:600;margin-top:2px}
.card.up .v{color:var(--up)}.card.empty .v{color:var(--empty)}.card.fail .v{color:var(--fail)}
table{width:calc(100% - 40px);margin:14px 20px 20px;border-collapse:collapse}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px;
position:sticky;top:0;background:var(--bg)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:hover td{background:#12181f}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;
letter-spacing:.3px}
.pill.reply{background:#12351f;color:var(--up)}
.pill.empty{background:#3a2f0e;color:var(--empty)}
.pill.fail{background:#3a1715;color:var(--fail)}
.mono{font-family:Consolas,"Cascadia Mono",monospace;font-size:12px}
.note{color:var(--dim);font-size:12px}
.note.err{color:var(--fail)}
.note.reason{color:var(--empty)}
.ok{color:var(--up)}
footer{padding:8px 20px 28px;color:var(--dim);font-size:12px;display:flex;
gap:10px;flex-wrap:wrap;align-items:center}
footer .sep{opacity:.4}
.empty-note{padding:24px 20px;color:var(--empty);font-size:14px}
"""


def load_i18n(lang="ru"):
    lang = lang if lang in LANGS else "ru"
    for cand in (lang, "ru"):
        try:
            with open(_pack_path(cand), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            continue
    return {}


def config_lang():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            lang = (json.load(fh) or {}).get("language")
        return lang if lang in LANGS else "ru"
    except Exception:
        return "ru"


def _t(T, key, default):
    return T.get(key, default)


def _esc(text):
    return html.escape(str(text if text is not None else ""), quote=True)


def _short(text, limit=120):
    return "".join(c if 32 <= ord(c) else " " for c in str(text)).strip()[:limit]


def render(results, meta, T=None):
    lang = meta.get("lang") if meta.get("lang") in LANGS else "ru"
    T = T if T is not None else load_i18n(lang)
    summary = {
        "reply": sum(1 for r in results if r["status"] == "reply"),
        "empty": sum(1 for r in results if r["status"] == "empty"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
        "total": len(results),
    }
    st_label = {
        "reply": _t(T, "st_reply", "REPLY"),
        "empty": _t(T, "st_empty", "EMPTY"),
        "fail": _t(T, "st_fail", "FAIL"),
    }
    rows = []
    for r in results:
        note = r["reply"] if r["status"] == "reply" else (r.get("error") or "")
        note_cls = "note" if r["status"] == "reply" else (
            "note reason" if r["status"] == "empty" else "note err")
        code = r.get("http_code") if r.get("http_code") else "-"
        rows.append(
            "<tr>"
            '<td><span class="pill %s">%s</span></td>'
            '<td class="mono">%s</td>'
            '<td class="mono">%s</td>'
            '<td class="num">%s</td>'
            '<td class="num">%s</td>'
            '<td class="%s">%s</td>'
            "</tr>" % (
                _esc(r["status"]), _esc(st_label.get(r["status"], "?")),
                _esc(r["model"]), _esc(r["host"]),
                _esc(r.get("ms", "")), _esc(code),
                note_cls, _esc(_short(note)),
            ))
    if not rows:
        body = '<div class="empty-note">%s</div>' % _esc(
            _t(T, "nothing", "nothing found"))
    else:
        body = (
            "<table><thead><tr>"
            '<th>%s</th><th>%s</th><th>%s</th><th class="num">%s</th>'
            '<th class="num">%s</th><th>%s</th>'
            "</tr></thead><tbody>%s</tbody></table>" % (
                _esc(_t(T, "col_st", "st")), _esc(_t(T, "col_model", "model")),
                _esc(_t(T, "col_host", "host")), _esc(_t(T, "col_ms", "ms")),
                _esc(_t(T, "col_http", "http")), _esc(_t(T, "col_note", "note")),
                "".join(rows)))

    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta.get("ts") or time.time()))
    return """<!DOCTYPE html>
<html lang="%(lang)s">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s - %(when)s</title>
<style>%(css)s</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>%(app)s</h1>
    <span class="tag">%(subtitle)s</span>
    <span class="when">%(when)s</span>
  </div>
</header>
<div class="meta">
  <span>%(l_agent)s: <b>%(agent)s</b></span>
  <span>%(l_source)s: <b class="p">%(source)s</b></span>
  <span>%(l_models)s: <b>%(nmodels)d</b></span>
  <span>%(l_providers)s: <b>%(nprov)d</b></span>
  <span>%(l_timeout)s: <b>%(timeout)gs</b></span>
  <span>%(l_maxtok)s: <b>%(maxtok)d</b></span>
  <span>%(l_workers)s: <b>%(workers)d</b></span>
  <span>%(l_elapsed)s: <b>%(elapsed).1fs</b></span>
</div>
<section class="cards">
  <div class="card up"><div class="k">%(c_reply)s</div><div class="v">%(n_reply)d</div></div>
  <div class="card empty"><div class="k">%(c_empty)s</div><div class="v">%(n_empty)d</div></div>
  <div class="card fail"><div class="k">%(c_fail)s</div><div class="v">%(n_fail)d</div></div>
  <div class="card"><div class="k">%(c_total)s</div><div class="v">%(n_total)d</div></div>
</section>
%(body)s
<footer>
  <span>%(legend)s</span>
  <span class="sep">|</span>
  <span>%(report_file)s: <span class="mono ok">%(report_path)s</span></span>
</footer>
</body>
</html>
""" % {
        "lang": _esc(lang),
        "title": _esc(_t(T, "app_name", "ModelPulse")),
        "app": _esc(_t(T, "app_name", "ModelPulse")),
        "subtitle": _esc(_t(T, "report_subtitle", "single pass report")),
        "when": _esc(when),
        "css": _CSS,
        "l_source": _esc(_t(T, "source", "source")),
        "source": _esc(meta.get("source") or ""),
        "l_agent": _esc(_t(T, "agent_word", "agent")),
        "agent": _esc(meta.get("agent") or ""),
        "l_models": _esc(_t(T, "models_word", "models")),
        "nmodels": meta.get("nmodels", len(results)),
        "l_providers": _esc(_t(T, "providers", "providers")),
        "nprov": meta.get("nproviders", 0),
        "l_timeout": _esc(_t(T, "timeout_word", "timeout")),
        "timeout": meta.get("timeout", 0),
        "l_maxtok": _esc(_t(T, "max_tokens", "max tokens")),
        "maxtok": meta.get("max_tokens", 0),
        "l_workers": _esc(_t(T, "workers_word", "workers")),
        "workers": meta.get("workers", 0),
        "l_elapsed": _esc(_t(T, "elapsed_word", "elapsed")),
        "elapsed": meta.get("elapsed", 0.0),
        "c_reply": _esc(_t(T, "card_reply", "replied")),
        "n_reply": summary["reply"],
        "c_empty": _esc(_t(T, "card_empty", "thinking")),
        "n_empty": summary["empty"],
        "c_fail": _esc(_t(T, "card_fail", "failed")),
        "n_fail": summary["fail"],
        "c_total": _esc(_t(T, "card_total", "total")),
        "n_total": summary["total"],
        "body": body,
        "legend": _esc(_t(T, "legend", "")),
        "report_file": _esc(_t(T, "report_file", "report file")),
        "report_path": _esc(meta.get("report_name") or ""),
    }, summary


def _prune(keep=KEEP_REPORTS):
    """Keep only the newest N timestamped reports."""
    try:
        files = sorted(
            (os.path.join(REPORT_DIR, f) for f in os.listdir(REPORT_DIR)
             if f.startswith("report-") and f.endswith(".html")),
            key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            os.remove(old)
    except Exception:
        pass


def write_report(results, meta, T=None):
    """Render, save a timestamped copy + last-run.html, return the main path."""
    meta = dict(meta)
    if meta.get("lang") not in LANGS:
        meta["lang"] = config_lang()
    T = T if T is not None else load_i18n(meta["lang"])
    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(meta.get("ts") or time.time()))
    main_path = os.path.join(REPORT_DIR, "report-%s.html" % stamp)
    meta["report_name"] = os.path.basename(main_path)
    html_out, summary = render(results, meta, T)
    with open(main_path, "w", encoding="utf-8") as fh:
        fh.write(html_out)
    # a stable alias so the browser can hold one bookmarkable page
    try:
        alias = os.path.join(REPORT_DIR, "last-run.html")
        with open(alias, "w", encoding="utf-8") as fh:
            fh.write(html_out)
    except Exception:
        pass
    _prune()
    return main_path, summary


# known install locations, newest families first; used only as a fallback
_KNOWN = [
    (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", "chromium"),
    (r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe", "chromium"),
    (r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe", "chromium"),
    (r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe", "chromium"),
    (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", "chromium"),
    (r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe", "chromium"),
    (r"%ProgramFiles%\Vivaldi\Application\vivaldi.exe", "chromium"),
    (r"%LOCALAPPDATA%\Programs\Opera\opera.exe", "chromium"),
    (r"%ProgramFiles%\Opera\opera.exe", "chromium"),
    (r"%ProgramFiles%\Yandex\YandexBrowser\Application\browser.exe", "chromium"),
    (r"%LOCALAPPDATA%\Yandex\YandexBrowser\Application\browser.exe", "chromium"),
    (r"%ProgramFiles%\Mozilla Firefox\firefox.exe", "firefox"),
    (r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe", "firefox"),
]


def _family_for(exe):
    name = os.path.basename(exe or "").lower()
    if name in ("firefox.exe", "waterfox.exe", "librewolf.exe"):
        return "firefox"
    if name:
        return "chromium"
    return "other"


def _parse_command(cmd):
    """Extract the executable path from a shell open command line."""
    cmd = (cmd or "").strip()
    if not cmd:
        return None
    if cmd[0] == '"':
        end = cmd.find('"', 1)
        if end > 0:
            return cmd[1:end]
    return cmd.split(" ", 1)[0]


def _default_browser_windows():
    """Ask Windows which browser handles http, then resolve its exe."""
    try:
        import winreg
    except Exception:
        return None
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\http\UserChoice") as k:
            progid = winreg.QueryValueEx(k, "ProgId")[0]
    except Exception:
        return None
    for root in (winreg.HKEY_CLASSES_ROOT,):
        try:
            with winreg.OpenKey(root, progid + r"\shell\open\command") as k:
                cmd = winreg.QueryValueEx(k, "")[0]
        except Exception:
            continue
        exe = _parse_command(cmd)
        if exe and os.path.isfile(exe):
            return exe
    return None


def browser_exe():
    """Resolve a browser executable, honoring an explicit override first.

    Priority: MODELPULSE_BROWSER env > config.json "browser" > Windows
    default browser > known install paths. Returns (exe_or_None, family).
    """
    override = (os.environ.get("MODELPULSE_BROWSER") or "").strip()
    if not override:
        try:
            override = (json.load(open(CONFIG_PATH, "r", encoding="utf-8"))
                        .get("browser") or "").strip()
        except Exception:
            override = ""
    if override:
        if os.path.isfile(override):
            return override, _family_for(override)
        # allow a bare family name: "firefox" / "chrome" / "edge"
        low = override.lower()
        for pat, fam in _KNOWN:
            if fam == low or low in os.path.basename(pat).lower():
                exp = os.path.expandvars(pat)
                if os.path.isfile(exp):
                    return exp, fam
    exe = _default_browser_windows()
    if exe:
        return exe, _family_for(exe)
    for pat, fam in _KNOWN:
        exp = os.path.expandvars(pat)
        if exp and os.path.isfile(exp):
            return exp, fam
    return None, None


def open_report(path):
    """Open the report in a separate browser window. Returns (ok, how)."""
    path = os.path.abspath(path)
    try:
        url = Path(path).resolve().as_uri()
    except Exception:
        url = path
    exe, fam = browser_exe()
    if exe:
        flag = "-new-window" if fam == "firefox" else "--new-window"
        try:
            subprocess.Popen([exe, flag, url])
            return True, os.path.basename(exe)
        except Exception:
            try:
                subprocess.Popen([exe, url])
                return True, os.path.basename(exe)
            except Exception:
                pass
    # last resort: whatever Windows has registered for .html
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)  # noqa: S606
            return True, "default"
        subprocess.Popen(["xdg-open", path])
        return True, "xdg-open"
    except Exception as e:
        return False, str(e)



if __name__ == "__main__":
    print(__doc__)
