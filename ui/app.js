// ModelPulse dashboard client (localized via /i18n.<lang>.json).

const $ = (s) => document.querySelector(s);
let MODELS = [];
let T = {};
let LANG = "ru";
let FILTER = "all";
let QUERY = "";
let PATHCFG = {};
let SORT = { key: "host", dir: 1 };

const PILL = { reply: "REPLY", empty: "EMPTY", fail: "FAIL" };
const LOCALES = { ru: "ru-RU", en: "en-US" };
const DATE_LOCALE = () => LOCALES[LANG] || "ru-RU";
const THEMES = ["light", "dark", "system"];
let THEME = "system";

function applyTheme(theme) {
  THEME = THEMES.includes(theme) ? theme : "system";
  document.documentElement.setAttribute("data-theme", THEME);
  document.querySelectorAll("#theme button").forEach((b) => {
    b.classList.toggle("on", b.dataset.theme === THEME);
  });
}

function setTheme(theme) {
  applyTheme(theme);
  try { localStorage.setItem("mp-theme", THEME); } catch (e) { /* private mode */ }
  fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ theme: THEME }),
  }).catch(() => {});
}

function t(key, fallback) {
  return (T && T[key]) || fallback || key;
}

function applyI18n() {
  document.documentElement.lang = LANG;
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const k = el.dataset.i18n;
    if (T[k]) el.textContent = T[k];
  });
  $("#filter").placeholder = t("filter_ph", "filter");
  $("#findq").placeholder = t("find_ph", "model id, e.g. glm-5.3-flash");
  $("#setpath").placeholder = t("settings_path_ph", "");
  $("#d-close").textContent = t("close", "close");
  document.querySelectorAll("#lang button").forEach((b) => {
    b.classList.toggle("on", b.dataset.lang === LANG);
  });
  const lt = $("#lang");
  if (lt) lt.setAttribute("aria-label", t("lang_title", "language"));
  document.querySelectorAll("#theme button").forEach((b) => {
    b.title = t("theme_" + b.dataset.theme, b.dataset.theme);
  });
  const tt = $("#theme");
  if (tt) tt.setAttribute("aria-label", t("theme_title", "theme"));
}

async function loadLangPack(lang) {
  try {
    const r = await fetch("/i18n." + lang + ".json", { cache: "no-store" });
    if (!r.ok) throw new Error("no pack");
    T = await r.json();
    return true;
  } catch (e) {
    if (lang !== "ru") return loadLangPack("ru");
    T = {};
    return false;
  }
}

async function setLang(lang) {
  const want = LOCALES[lang] ? lang : "ru";
  LANG = want;
  if (!(await loadLangPack(want))) LANG = "ru";
  applyI18n();
  render();
  refresh();
  const activeId = (SOURCES.find((s) => s.active) || {}).id;
  renderSources(activeId);
  setActiveLabel(activeId);
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: LANG }),
    });
  } catch (e) { /* language still applies locally */ }
}

function pill(status) {
  return `<span class="pill ${status}">${PILL[status] || "?"}</span>`;
}

function fmtMs(ms) {
  if (ms == null) return "-";
  return ms >= 1000 ? (ms / 1000).toFixed(1) + t("sec_short", "s") : ms + "ms";
}

function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtDate(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString(DATE_LOCALE());
}

function upBar(u) {
  if (!u) return `<span class="note">${t("no_data", "no data")}</span>`;
  return `<span class="upbar"><span class="u">${u.reply_pct}%</span>
    <span class="note">(${u.reply}/${u.total})</span></span>`;
}

function spark(hist) {
  if (!hist || !hist.length) return "";
  const recent = hist.slice(-40);
  const max = Math.max(200, ...recent.map((h) => h.ms || 0));
  return `<div class="spark">` + recent.map((h) => {
    const hgt = h.status === "reply"
      ? Math.max(3, Math.round(16 * (h.ms || 1) / max)) : 16;
    return `<i class="${h.status}" style="height:${hgt}px" title="${fmtTime(h.ts)} ${h.status} ${fmtMs(h.ms)}"></i>`;
  }).join("") + `</div>`;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function note(r) {
  if (r.status === "reply") return `<span class="note mono">${esc(r.reply || "")}</span>`;
  const code = r.note_code || (r.status === "empty" ? "empty" : "error");
  const label = t("note_" + code, "");
  const raw = r.note_text || r.error || "";
  const title = raw ? esc(label ? label + " - " + raw : raw) : esc(label || code);
  return `<span class="note ${r.status === "empty" ? "reason" : "err"}" title="${title}">`
    + esc(label || raw || code) + `</span>`;
}

// reason texts live in the lang pack under dead_<code>; fall back to server text
function reasonText(r) {
  return t("dead_" + (r.reason_code || ""), r.reason_text || "") || "";
}

function deadBadge(r) {
  if (!r.dead) return "";
  const txt = reasonText(r);
  return `<span class="badge-dead" title="${esc(txt)}">${t("dead_word", "dead")}</span>`;
}

function actBtn(r) {
  const hide = r.hidden
    ? `<button class="act ghost unhide" data-act="unhide">${t("unhide", "restore")}</button>`
    : `<button class="act ghost hide" data-act="hide">${t("hide", "hide")}</button>`;
  const del = r.provider_id
    ? `<button class="act ghost del" data-act="del">${t("del_word", "delete")}</button>`
    : "";
  return hide + del;
}

function passes(r) {
  if (FILTER === "hidden") {
    if (!r.hidden) return false;
  } else {
    if (r.hidden) return false;          // muted rows leave the normal list
    if (FILTER === "dead" && !r.dead) return false;
    if (FILTER !== "all" && FILTER !== "dead" && r.status !== FILTER) return false;
  }
  if (QUERY) {
    const hay = (r.model + " " + r.host + " " + (r.provider_id || "")).toLowerCase();
    if (!hay.includes(QUERY)) return false;
  }
  return true;
}

const RANK = { reply: 0, empty: 1, fail: 2 };

function sortValue(r, k) {
  switch (k) {
    case "status": return RANK[r.status] ?? 3;
    case "dead": return (r.hidden ? 2 : 0) + (r.dead ? 1 : 0);
    case "uptime": {
      const u = r.uptime_24h;
      return u ? u.reply_pct : -1;
    }
    case "history": {
      const h = r.history || [];
      return h.reduce((s, x) => s + (x.status === "reply" ? 1 : 0), 0);
    }
    case "note":
      if (r.status === "reply") return String(r.reply || "").toLowerCase();
      return String(t("note_" + (r.note_code || "error"), "")
        || r.note_text || r.error || "").toLowerCase();
    case "http_code": return r.http_code == null ? -1 : r.http_code;
    case "ms": return r.ms == null ? -1 : r.ms;
    default: return r[k];
  }
}

function cmp(a, b) {
  const k = SORT.key;
  let va = sortValue(a, k), vb = sortValue(b, k);
  if (typeof va === "string" || typeof vb === "string") {
    va = String(va || "").toLowerCase();
    vb = String(vb || "").toLowerCase();
    return va < vb ? -SORT.dir : va > vb ? SORT.dir : 0;
  }
  va = va == null ? -1 : va;
  vb = vb == null ? -1 : vb;
  return (va - vb) * SORT.dir;
}

function markSort() {
  document.querySelectorAll("#grid thead th[data-sort]").forEach((th) => {
    th.classList.remove("asc", "desc");
    if (th.dataset.sort === SORT.key) {
      th.classList.add(SORT.dir > 0 ? "asc" : "desc");
    }
  });
}

function render() {
  const rows = MODELS.filter(passes).sort(cmp);
  $("#rows").innerHTML = rows.map((r) => {
    const u24 = r.uptime_24h, u1 = r.uptime_1h;
    const cls = ["clickable", r.hidden ? "muted" : "", r.dead ? "dead" : ""]
      .filter(Boolean).join(" ");
    return `<tr class="${cls}" data-model="${esc(r.model)}" data-host="${esc(r.host)}">
      <td><div class="stcell">${pill(r.status)}${deadBadge(r)}</div></td>
      <td class="mono">${esc(r.model)}</td>
      <td class="note">${esc(r.host)}</td>
      <td class="num">${fmtMs(r.ms)}</td>
      <td class="num">${r.http_code || "-"}</td>
      <td>${u1 ? upBar(u1) : "-"} <span class="note">/</span> ${u24 ? upBar(u24) : "-"}</td>
      <td>${spark(r.history)}</td>
      <td>${note(r)}</td>
      <td class="act">${actBtn(r)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="9" class="note">${t("nothing", "nothing")}</td></tr>`;

  markSort();
  document.querySelectorAll("tr[data-model]").forEach((tr) => {
    tr.onclick = () => openDrawer(tr.dataset.model, tr.dataset.host);
    tr.querySelectorAll(".act button").forEach((b) => {
      b.onclick = (e) => {
        e.stopPropagation();
        const r = MODELS.find((m) => m.model === tr.dataset.model
          && m.host === tr.dataset.host);
        if (!r) return;
        if (b.dataset.act === "del") deleteFromAgent(r);
        else toggleHidden(r);
      };
    });
  });
}

function findRow(h) {
  const editable = h.editable;
  const role = h.role === "default" ? t("find_role_default", "default")
    : t("find_role_list", "in list");
  const alias = h.alias ? ` <span class="note">(${esc(h.alias)})</span>` : "";
  const acts = editable
    ? `<button class="act ghost copy" data-copy="${esc(h.how)}">${t("find_copy", "copy address")}</button>
       <button class="act ghost del" data-del="1">${t("del_word", "delete")}</button>`
    : `<span class="tag ro">${t("find_readonly", "metadata")}</span>`;
  return `<div class="find-row ${editable ? "" : "noread"}" data-idx="${h.__i}">
    <div class="find-main">
      <div class="find-top">
        <span class="fagent">${esc(h.agent)}</span>
        <span class="fmodel">${esc(h.model)}</span>${alias}
        <span class="note">${role}</span>
      </div>
      <div class="find-bot">
        <span class="fhow ${editable ? "" : "meta"}">${esc(h.how || "-")}</span>
        <span class="fpath" title="${esc(h.path)}">${esc(h.path)}</span>
      </div>
    </div>
    <div class="acts">${acts}</div>
  </div>`;
}

let FIND_HITS = [];

function renderFind(hits, query) {
  const list = $("#findlist");
  FIND_HITS = hits;
  hits.forEach((h, i) => { h.__i = i; });
  if (!query) { list.classList.add("hidden"); list.innerHTML = ""; return; }
  list.classList.remove("hidden");
  if (!hits.length) {
    list.innerHTML = `<div class="find-note">${t("find_none", "not found in any config")}</div>`;
    return;
  }
  list.innerHTML = hits.map(findRow).join("");
  list.querySelectorAll(".find-row").forEach((row) => {
    const h = FIND_HITS[Number(row.dataset.idx)];
    const cp = row.querySelector("[data-copy]");
    if (cp) cp.onclick = (e) => {
      e.stopPropagation();
      navigator.clipboard && navigator.clipboard.writeText(h.how);
      $("#findstate").textContent = t("find_copied", "address copied");
      setTimeout(() => { $("#findstate").textContent = ""; }, 1500);
    };
    const dl = row.querySelector("[data-del]");
    if (dl) dl.onclick = (e) => { e.stopPropagation(); deleteFound(h); };
  });
}

async function runFind() {
  const q = $("#findq").value.trim();
  if (!q) { renderFind([], ""); return; }
  const meta = $("#findmeta").checked ? "1" : "0";
  $("#findstate").textContent = t("state_scanning", "searching...");
  try {
    const j = await (await fetch(`/api/find-model?q=${encodeURIComponent(q)}&meta=${meta}`,
      { cache: "no-store" })).json();
    $("#findstate").textContent =
      `${j.count} ${t("find_hits", "places")}`;
    renderFind(j.hits || [], q);
  } catch (e) {
    $("#findstate").textContent = String(e);
  }
}

// delete one found occurrence - only ever after the client approves the
// exact spot shown in the row; never a bulk or silent edit
async function deleteFound(h) {
  const msg = t("find_confirm", "Delete this model from the agent's config?")
    + "\n\n" + h.agent + "  -  " + h.model
    + "\n" + t("find_confirm_where", "file:") + " " + h.path
    + "\n" + t("find_confirm_addr", "address:") + " " + (h.how || "-")
    + "\n\n" + t("confirm_delete_note", "A backup is kept; history stays.");
  if (!window.confirm(msg)) return;
  let j = {};
  try {
    const res = await fetch("/api/delete-model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: h.model, provider_id: h.provider_id,
        source_id: h.source_id || "", host: h.host || "" }),
    });
    j = await res.json();
  } catch (e) { j = { ok: false, error: String(e) }; }
  if (!j.ok) { window.alert(t("delete_failed", "delete failed") + ": " + (j.error || "")); return; }
  $("#findstate").textContent = t("find_deleted", "deleted")
    + (j.backup ? "  (" + t("find_backup", "backup") + ")" : "");
  runFind();
  refresh();
}

async function toggleHidden(r) {
  const goHide = !r.hidden;
  if (goHide) {
    const msg = r.dead
      ? t("confirm_hide_dead", "Hide this model?") + "\n\n" + reasonText(r)
      : t("confirm_hide", "Hide this model from the list?");
    if (!window.confirm(msg)) return;
  }
  const url = goHide ? "/api/hide" : "/api/unhide";
  const body = { model: r.model, host: r.host };
  if (goHide) {
    body.reason_code = r.reason_code || "";
    body.reason_text = r.reason_text || "";
    body.provider_id = r.provider_id || "";
  }
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await loadModels();
}

async function deleteFromAgent(r) {
  const msg = t("confirm_delete", "Delete this model from the agent's list?")
    + "\n\n" + r.model + "  @" + r.host
    + "\n" + t("confirm_delete_note", "A backup of settings.json is kept; history stays.");
  if (!window.confirm(msg)) return;
  let j = {};
  try {
    const res = await fetch("/api/delete-model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: r.model, host: r.host, provider_id: r.provider_id }),
    });
    j = await res.json();
  } catch (e) { j = { ok: false, error: String(e) }; }
  if (!j.ok) { window.alert(t("delete_failed", "delete failed") + ": " + (j.error || "")); return; }
  await loadModels();
  await loadSources();
}

async function loadModels() {
  const j = await (await fetch("/api/models")).json();
  MODELS = j.models || [];
  render();
}

function setCards(last) {
  $("#c-reply").textContent = last ? last.replied : "-";
  $("#c-empty").textContent = last ? last.empty : "-";
  $("#c-fail").textContent = last ? last.failed : "-";
  $("#c-total").textContent = last ? last.total : "-";
}

let SCANNING = false;
let SCAN_HOLD = 0;
let CANCELLING = false;
let CANCELLED_SHOWN = false;

function setScanning(on, sub) {
  const ov = $("#scan-overlay");
  if (!ov) return;
  if (on) {
    CANCELLED_SHOWN = false;
    $("#scan-sub").textContent = sub || "";
    $("#scan-hint").textContent = t("cancel_hint", "Esc");
    const cb = $("#scan-cancel");
    cb.textContent = t("cancel", "Cancel");
    cb.disabled = false;
    ov.classList.remove("hidden");
  } else {
    ov.classList.add("hidden");
    CANCELLING = false;
  }
}

function scanSub(j) {
  const who = activeLabel(j.active) || j.agent || "";
  const n = j.models ? j.models + " " + t("models_word", "models") : "";
  return [who, n].filter(Boolean).join(" - ");
}

async function cancelScan() {
  if (!SCANNING || CANCELLING) return;
  CANCELLING = true;
  SCAN_HOLD = 0;
  const cb = $("#scan-cancel");
  if (cb) { cb.disabled = true; cb.textContent = t("cancelling", "cancelling..."); }
  $("#scanstate").textContent = t("cancelling", "cancelling...");
  try {
    await fetch("/api/cancel", { method: "POST" });
  } catch (e) { /* the next poll will reveal the state */ }
  setTimeout(refresh, 400);
}

async function loadSource() {
  const j = await (await fetch("/api/source")).json();
  const now = Date.now();
  const show = !!j.scanning || now < SCAN_HOLD;
  SCANNING = show;
  setScanning(show, scanSub(j));
  $("#c-source").textContent = j.path || "-";
  $("#c-source-meta").textContent =
    `${j.agent || "-"} - ${t("providers", "providers")}: ${j.providers} - ${t("models_word", "models")}: ${j.models}`
    + ` - ${t("reread", "reread")}: ${fmtDate(j.mtime)}`;
  $("#interval").value = j.interval;
  const dot = $("#livedot");
  dot.className = "dot " + (j.scanning ? "busy" : "live");
  if (j.scanning) {
    $("#scanstate").textContent = t("state_scanning", "scanning...");
  } else if (j.last_cancelled && !CANCELLED_SHOWN) {
    CANCELLED_SHOWN = true;
    $("#scanstate").textContent = t("cancelled", "cancelled");
    setTimeout(() => { CANCELLED_SHOWN = false; }, 5000);
  } else if (!CANCELLED_SHOWN) {
    $("#scanstate").textContent = j.last_error
      ? t("state_error", "error") + ": " + j.last_error
      : t("state_idle", "idle");
  }
  if (j.scanning) setTimeout(refresh, 1500);
  return j;
}

let SOURCES = [];

function activeLabel(activeId) {
  const s = SOURCES.find((x) => x.id === activeId);
  return s ? s.agent : (activeId || "-");
}

function setActiveLabel(activeId) {
  $("#srcactive").innerHTML = t("active_source", "active") + ": <b>"
    + esc(activeLabel(activeId)) + "</b>";
}

function srcTag(s) {
  const out = [];
  if (s.probeable) out.push(`<span class="tag ok">${t("tag_probe", "probe")}</span>`);
  else out.push(`<span class="tag meta">${t("tag_list", "list only")}</span>`);
  if (s.has_keys) out.push(`<span class="tag keys">${t("tag_keys", "has key")}</span>`);
  if (s.category === "registry") out.push(`<span class="tag meta">${t("tag_meta", "registry")}</span>`);
  else if (s.category === "catalog") out.push(`<span class="tag meta">${t("tag_catalog", "catalog")}</span>`);
  return out.join(" ");
}

function renderSources(active) {
  const list = $("#srclist");
  if (!SOURCES.length) {
    list.innerHTML = `<div class="src-note">${t("nothing", "nothing")}</div>`;
    return;
  }
  list.innerHTML = SOURCES.map((s) => `
    <div class="src-row ${s.active ? "on" : ""}${s.probeable ? "" : " noprobe"}" data-id="${esc(s.id)}">
      <span class="dotmark">${s.active ? "\u25cf" : "\u25cb"}</span>
      <span class="sname">${esc(s.agent)}${s.probeable ? "" : ' <span class="note">(' + esc(t("warn_notprobeable", "cannot probe")) + ')</span>'}</span>
      <span class="spath" title="${esc(s.path)}">${esc(s.path)}</span>
      <span class="scount">${s.model_count}</span>
      <span class="stag">${srcTag(s)}</span>
    </div>
    ${s.note && s.category !== "agent" ? `<div class="src-note">${esc(s.note)}</div>` : ""}`
  ).join("");
  list.querySelectorAll(".src-row").forEach((row) => {
    row.onclick = () => selectSource(row.dataset.id);
  });
}

function whyNotProbeable(s) {
  if (!s) return t("empty_notprobeable", "cannot probe this source");
  if (s.category === "registry") return t("empty_registry", "registry (metadata)");
  if (!s.has_keys) return t("empty_nokeys", "no API key");
  if (!s.probeable) return t("empty_notprobeable", "not OpenAI-compatible");
  return t("empty_filter", "nothing to probe");
}

async function loadSources(deep) {
  const q = deep === undefined ? "" : ("?deep=" + (deep ? "1" : "0"));
  $("#srcstate").textContent = t("state_scanning", "scanning...");
  try {
    const r = await fetch("/api/sources" + q, { cache: "no-store" });
    if (!r.ok) {
      $("#srcstate").textContent = t("server_stale", "restart the server (old version running)");
      return;
    }
    const j = await r.json();
    if (!j || !Array.isArray(j.sources)) {
      $("#srcstate").textContent = t("server_stale", "restart the server (old version running)");
      return;
    }
    SOURCES = j.sources;
    $("#srcbadge").textContent = SOURCES.length;
    const probe = SOURCES.filter((s) => s.probeable).length;
    $("#srcstate").textContent =
      `${t("found", "found")}: ${SOURCES.length} (${t("probeable", "probeable")}: ${probe})`;
    renderSources(j.active);
    setActiveLabel(j.active);
  } catch (e) {
    $("#srcstate").textContent = String(e);
  }
}

async function selectSource(id) {
  const s = SOURCES.find((x) => x.id === id);
  if (s && !s.probeable) {
    $("#srcstate").textContent =
      t("warn_notprobeable", "не прозвонить") + ": " + whyNotProbeable(s);
  } else {
    $("#srcstate").textContent = "...";
  }
  try {
    const r = await fetch("/api/source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active: id }),
    });
    const j = await r.json();
    id = j.active || id;
  } catch (e) {
    $("#srcstate").textContent = String(e);
    return;
  }
  SOURCES.forEach((x) => { x.active = (x.id === id); });
  renderSources(id);
  setActiveLabel(id);
  if (!(s && !s.probeable)) $("#srcstate").textContent = t("scan_done", "done");
  setTimeout(refresh, 800);
}

async function loadConfig() {
  const j = await (await fetch("/api/config")).json();
  PATHCFG = j;
  $("#setpath").value = j.override || "";
  $("#maxtok").value = (j.config && j.config.max_tokens) || 4096;
  $("#setpath").placeholder = t("settings_path_ph", j.default_path || "");
}

async function loadSummary() {
  const j = await (await fetch("/api/summary")).json();
  setCards(j.last_run);
  const lr = j.last_run;
  if (lr) {
    const trg = t("trigger_" + lr.trigger, lr.trigger);
    $("#foot").textContent =
      `${t("last_scan", "last scan")} ${fmtTime(lr.finished_at || lr.started_at)} - ${t("trigger", "trigger")}: ${trg}`;
  }
}

function refresh() {
  loadSource();
  loadSummary();
  loadModels();
}

async function openDrawer(model, host) {
  const j = await (await fetch("/api/model/" + encodeURIComponent(model)
    + "?host=" + encodeURIComponent(host || ""))).json();
  $("#d-title").textContent = model + (host ? "  @" + host : "");
  const u = j.uptime_24h;
  const head = u
    ? `<div class="drow"><span class="t">${t("uptime_24h", "uptime 24h")}</span>
        <span>${u.reply_pct}% ${t("reply_word", "replies")} (${u.reply}/${u.total}),
        ${u.empty} ${t("empty_word", "empty")}, ${u.fail} ${t("fail_word", "failed")}</span></div>`
    : `<div class="drow"><span class="t">${t("uptime_24h", "uptime 24h")}</span>
        <span>${t("no_data", "no data")}</span></div>`;
  const rows = (j.probes || []).map((p) => {
    const detail = p.status === "reply" ? esc(p.reply || "")
      : esc(t("note_" + (p.note_code || "error"), "") || p.note_text
        || p.error || "HTTP " + (p.http_code || "?"));
    return `<div class="drow"><span class="t">${fmtDate(p.ts)}</span>
      <span>${pill(p.status)} ${fmtMs(p.ms)} ${detail}</span></div>`;
  }).join("");
  $("#d-body").innerHTML = head
    + `<div style="margin-top:10px">${rows || t("no_history", "no history")}</div>`;
  $("#drawer").classList.remove("hidden");
}

$("#d-close").onclick = () => $("#drawer").classList.add("hidden");

$("#scan-cancel").onclick = () => cancelScan();

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#drawer").classList.contains("hidden")) {
    $("#drawer").classList.add("hidden");
    return;
  }
  if (SCANNING) cancelScan();
});

$("#runnow").onclick = async () => {
  $("#scanstate").textContent = t("state_starting", "starting...");
  SCAN_HOLD = Date.now() + 4000;
  CANCELLING = false;
  setScanning(true, t("state_starting", "starting..."));
  await fetch("/api/run", { method: "POST" });
  setTimeout(refresh, 900);
};

$("#saveset").onclick = async () => {
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      interval: Number($("#interval").value),
      max_tokens: Number($("#maxtok").value),
    }),
  });
  refresh();
};

$("#savepath").onclick = async () => {
  $("#pathstate").textContent = "...";
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings_path: $("#setpath").value.trim() }),
  });
  $("#pathstate").textContent = t("scan_done", "done");
  setTimeout(() => { $("#pathstate").textContent = ""; refresh(); }, 800);
};

$("#openapp").onclick = async () => {
  const j = await (await fetch("/api/open-folder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ which: "app" }),
  })).json();
  $("#pathstate").textContent = j.ok ? t("scan_done", "done") : (j.error || "");
  setTimeout(() => { $("#pathstate").textContent = ""; }, 1500);
};

$("#filter").oninput = (e) => {
  QUERY = e.target.value.trim().toLowerCase();
  render();
};

$("#findgo").onclick = () => runFind();
$("#findq").onkeydown = (e) => { if (e.key === "Enter") runFind(); };
$("#findmeta").onchange = () => { if ($("#findq").value.trim()) runFind(); };

document.querySelectorAll(".chip").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll(".chip").forEach((x) => x.classList.remove("on"));
    c.classList.add("on");
    FILTER = c.dataset.f;
    render();
  };
});

document.querySelectorAll("th[data-sort]").forEach((th) => {
  th.onclick = () => {
    const k = th.dataset.sort;
    if (SORT.key === k) SORT.dir *= -1;
    else SORT = { key: k, dir: 1 };
    render();
  };
});

$("#srctoggle").onclick = () => {
  $(".sources").classList.toggle("open");
  $("#srclist").classList.toggle("hidden");
  if (!$("#srclist").classList.contains("hidden") && !SOURCES.length) {
    loadSources($("#deepscan").checked);
  }
};

$("#srcrescan").onclick = () => loadSources($("#deepscan").checked);

$("#deepscan").onchange = () => loadSources($("#deepscan").checked);

document.querySelectorAll("#lang button").forEach((b) => {
  b.onclick = () => { if (b.dataset.lang !== LANG) setLang(b.dataset.lang); };
});

document.querySelectorAll("#theme button").forEach((b) => {
  b.onclick = () => { if (b.dataset.theme !== THEME) setTheme(b.dataset.theme); };
});

// follow the OS live while the user keeps "system" selected
if (window.matchMedia) {
  const mq = window.matchMedia("(prefers-color-scheme: light)");
  const onScheme = () => { if (THEME === "system") applyTheme("system"); };
  if (mq.addEventListener) mq.addEventListener("change", onScheme);
  else if (mq.addListener) mq.addListener(onScheme);
}

(async () => {
  // theme: the inline head script already applied localStorage; sync the buttons
  applyTheme(document.documentElement.getAttribute("data-theme") || "system");
  let saved = "ru";
  let savedTheme = "";
  try {
    const c = await (await fetch("/api/config", { cache: "no-store" })).json();
    saved = (c.config && c.config.language) || "ru";
    savedTheme = (c.config && c.config.theme) || "";
  } catch (e) { /* fall back to defaults */ }
  // first run on this browser: adopt the theme stored in config.json
  if (!localStorage.getItem("mp-theme") && THEMES.includes(savedTheme)) {
    applyTheme(savedTheme);
  }
  LANG = LOCALES[saved] ? saved : "ru";
  await loadLangPack(LANG);
  applyI18n();
  await loadConfig();
  refresh();
  loadSources(false);
  setInterval(refresh, 10000);
})();
