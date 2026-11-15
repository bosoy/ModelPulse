// ModelPulse dashboard client (localized via /i18n.ru.json).

const $ = (s) => document.querySelector(s);
let MODELS = [];
let T = {};
let FILTER = "all";
let QUERY = "";
let PATHCFG = {};
let SORT = { key: "host", dir: 1 };

const PILL = { reply: "REPLY", empty: "EMPTY", fail: "FAIL" };

function t(key, fallback) {
  return (T && T[key]) || fallback || key;
}

function applyI18n() {
  document.documentElement.lang = "ru";
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const k = el.dataset.i18n;
    if (T[k]) el.textContent = T[k];
  });
  $("#filter").placeholder = t("filter_ph", "filter");
  $("#setpath").placeholder = t("settings_path_ph", "");
  $("#d-close").textContent = t("close", "close");
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
  return new Date(ts * 1000).toLocaleString("ru-RU");
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
  if (r.status === "empty") return `<span class="note reason">${esc(r.error || "no text")}</span>`;
  const code = r.http_code ? "HTTP " + r.http_code + " " : "";
  return `<span class="note err">${code}${esc(r.error || "")}</span>`;
}

function passes(r) {
  if (FILTER !== "all" && r.status !== FILTER) return false;
  if (QUERY) {
    const hay = (r.model + " " + r.host + " " + (r.provider_id || "")).toLowerCase();
    if (!hay.includes(QUERY)) return false;
  }
  return true;
}

function cmp(a, b) {
  const k = SORT.key;
  let va = a[k], vb = b[k];
  if (k === "model" || k === "host") {
    va = String(va || "").toLowerCase();
    vb = String(vb || "").toLowerCase();
    return va < vb ? -SORT.dir : va > vb ? SORT.dir : 0;
  }
  va = va == null ? -1 : va;
  vb = vb == null ? -1 : vb;
  return (va - vb) * SORT.dir;
}

function render() {
  const rows = MODELS.filter(passes).sort(cmp);
  $("#rows").innerHTML = rows.map((r) => {
    const u24 = r.uptime_24h, u1 = r.uptime_1h;
    return `<tr class="clickable" data-model="${esc(r.model)}" data-host="${esc(r.host)}">
      <td>${pill(r.status)}</td>
      <td class="mono">${esc(r.model)}</td>
      <td class="note">${esc(r.host)}</td>
      <td class="num">${fmtMs(r.ms)}</td>
      <td class="num">${r.http_code || "-"}</td>
      <td>${u1 ? upBar(u1) : "-"} <span class="note">/</span> ${u24 ? upBar(u24) : "-"}</td>
      <td>${spark(r.history)}</td>
      <td>${note(r)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="note">${t("nothing", "nothing")}</td></tr>`;

  document.querySelectorAll("tr[data-model]").forEach((tr) => {
    tr.onclick = () => openDrawer(tr.dataset.model, tr.dataset.host);
  });
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

async function loadSource() {
  const j = await (await fetch("/api/source")).json();
  $("#c-source").textContent = j.path || "-";
  $("#c-source-meta").textContent =
    `${j.agent || "-"} - ${t("providers", "providers")}: ${j.providers} - ${t("models_word", "models")}: ${j.models}`
    + ` - ${t("reread", "reread")}: ${fmtDate(j.mtime)}`;
  $("#interval").value = j.interval;
  const dot = $("#livedot");
  dot.className = "dot " + (j.scanning ? "busy" : "live");
  $("#scanstate").textContent = j.scanning
    ? t("state_scanning", "scanning...")
    : (j.last_error ? t("state_error", "error") + ": " + j.last_error
                    : t("state_idle", "idle"));
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
      <span class="sname">${esc(s.agent)}${s.probeable ? "" : ' <span class="note">(не прозвонить)</span>'}</span>
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
  $("#maxtok").value = (j.config && j.config.max_tokens) || 16;
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
      : esc(p.error || "HTTP " + (p.http_code || "?"));
    return `<div class="drow"><span class="t">${fmtDate(p.ts)}</span>
      <span>${pill(p.status)} ${fmtMs(p.ms)} ${detail}</span></div>`;
  }).join("");
  $("#d-body").innerHTML = head
    + `<div style="margin-top:10px">${rows || t("no_history", "no history")}</div>`;
  $("#drawer").classList.remove("hidden");
}

$("#d-close").onclick = () => $("#drawer").classList.add("hidden");

$("#runnow").onclick = async () => {
  $("#scanstate").textContent = t("state_starting", "starting...");
  await fetch("/api/run", { method: "POST" });
  setTimeout(refresh, 1200);
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

(async () => {
  try {
    T = await (await fetch("/i18n.ru.json")).json();
  } catch (e) { T = {}; }
  applyI18n();
  await loadConfig();
  refresh();
  loadSources(false);
  setInterval(refresh, 10000);
})();
