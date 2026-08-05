// Control Tower — vanilla JS, fetch + polling (same style as the GT app).
// The batch journey: uploaded → parsing → predicted → awaiting_annotation →
// annotated → delivering → delivered. Every error is an event and visible here.

// ── State ───────────────────────────────────────────────────────────────────
const state = {
  dayId: null,
  tab: "batches",
  pollTimer: null,
  lastFunnel: null,   // previous counts, to bump/animate stages that changed
  deferred: [],       // current deferred list for the redeliver dialog
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ── Timestamps ──────────────────────────────────────────────────────────────
// Everything is STORED in UTC (the warehouse session is Etc/UTC and Lakebase
// keeps UTC too) but arrives in two shapes: "2026-08-04T08:54:01.971Z" from
// StatementExecution, and a naive "2026-08-04 08:54:01" from psycopg — see
// stringify() in core/pg.py. A string without an offset is UTC by
// construction, so pin the Z explicitly: new Date("2026-08-04 08:54:01")
// parses as LOCAL time, which would shift the clock by the current Rome offset
// and look plausible enough that nobody would notice.
// sv-SE formats as "YYYY-MM-DD HH:MM:SS", keeping the layout the UI already
// had; only the zone changes. Intl handles the CET/CEST switch by itself.
const TZ = "Europe/Rome";
const _TS_FMT = new Intl.DateTimeFormat("sv-SE", {
  timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
function fmtTs(v) {
  const s = String(v ?? "").trim();
  if (!s) return "";
  const iso = s.replace(" ", "T");
  const d = new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : iso + "Z");
  return isNaN(d.getTime()) ? s.slice(0, 19) : _TS_FMT.format(d);
}

// ── Errors tab badge ────────────────────────────────────────────────────────
// Blinks while the selected batch has files that still need a human. Those
// files also BLOCK delivery (run-deliver 409s), so the signal has to be
// visible from every tab, not only from Errors — otherwise the operator finds
// out only when the SFTP button refuses.
// Fed from whatever data the app already has (batches list, funnel, errors
// list); no extra polling — /api/days is the N+1 endpoint we must not hammer.
function setErrorBadge(n) {
  const el = $("tab-errors-badge");
  if (!el) return;
  const count = Number(n) || 0;
  el.textContent = count > 99 ? "99+" : String(count);
  el.classList.toggle("hidden", count === 0);
  const btn = el.closest(".tab");
  if (btn) {
    btn.classList.toggle("has-errors", count > 0);
    btn.title = count
      ? `${count} file da sistemare a mano — la consegna SFTP è bloccata finché `
        + `non li annoti nel tab Manual`
      : "";
  }
}

// ── API ─────────────────────────────────────────────────────────────────────
const jget = (url) => fetch(url).then((r) => r.json());
const jpost = (url, body) =>
  fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => r.json());
const dayQ = () => `day_id=${encodeURIComponent(state.dayId || "")}`;

// ── Toast ───────────────────────────────────────────────────────────────────
let toastTimer = null;
function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 5000);
}

// ── Leaving the annotate tab ────────────────────────────────────────────────
// window.Annotate (not the const) so this is safe to call before annotate.js
// has finished parsing.
function canLeaveAnnotate() {
  if (!window.Annotate || !window.Annotate.isDirty()) return true;
  return confirm("Unsaved annotation. Discard your marks?");
}

// ── Tabs ────────────────────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach((b) => {
  b.onclick = () => {
    const leavingAnnotate = state.tab === "annotate" && b.dataset.tab !== "annotate";
    if (leavingAnnotate && !canLeaveAnnotate()) return;
    if (leavingAnnotate) window.Annotate.leave();

    state.tab = b.dataset.tab;
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".panel").forEach((p) =>
      p.classList.toggle("active", p.id === `panel-${state.tab}`));
    refreshTab();
    schedulePoll();
  };
});

function refreshTab() {
  const fn = {
    batches: loadBatches,
    flow: loadFlow,
    gate: loadGate,
    annotate: () => window.Annotate && window.Annotate.enter(),
    errors: loadErrors,
    sftp: loadSftp,
    review: loadReview,
    files: loadFiles,
  }[state.tab];
  if (fn) fn();
}

// ── Batch selection ─────────────────────────────────────────────────────────
// The ONLY writer of state.dayId. Every entry point goes through it, so an
// unsaved annotation can never have its batch swapped out from under it.
function setDay(id) {
  if (!id || id === state.dayId) return true;
  if (!canLeaveAnnotate()) return false;
  state.dayId = id;
  $("day-select").value = id;
  state.lastFunnel = null;   // don't animate against another batch's numbers
  resetFunnelTweens();       // nor tween from them: 1480 → 4 is not progress
  if (window.Annotate) window.Annotate.onDayChange();
  return true;
}

// Switch batch + tab together. Carrying the day_id is the whole point: the old
// two-app link opened the annotator at its root and let it pick its own batch.
// Returns false when the switch was refused (unsaved annotation), so a caller
// that wants to do something ON the target tab knows it never got there.
function goTab(tab, dayId) {
  if (dayId && !setDay(dayId)) return false;
  document.querySelector(`[data-tab="${tab}"]`).click();
  return true;
}
window.goTab = goTab;

// ── Day selector ────────────────────────────────────────────────────────────
async function loadDays(pickFirst = false) {
  const res = await jget("/api/days");
  if (res.error) return toast(`Batches: ${res.error}`, true);
  const days = res.days || [];
  const sel = $("day-select");
  const prev = state.dayId;
  sel.innerHTML = "";
  days.forEach((d) => {
    const o = document.createElement("option");
    o.value = d.day_id;
    o.textContent = `${d.day_id} — ${d.lifecycle}`;
    sel.appendChild(o);
  });
  if (days.length) {
    // Keep the current batch if it's still listed, else fall back to the newest.
    // Through setDay so a batch that disappeared also resets the annotate tab
    // instead of leaving it pointed at a batch that no longer exists.
    const keep = prev && days.some((d) => d.day_id === prev);
    setDay(keep ? prev : days[0].day_id);
    sel.value = state.dayId;
  }
  const cur = days.find((d) => d.day_id === state.dayId);
  if (cur) setErrorBadge((cur.counts || {}).n_delivery_blocked);
  renderBatches(days);
  return days;
}
$("day-select").onchange = () => {
  // Revert the <select> if the switch is refused — otherwise it displays a
  // batch the app is not actually on.
  if (!setDay($("day-select").value)) {
    $("day-select").value = state.dayId;
    return;
  }
  refreshTab();
};

// ── Batches panel ───────────────────────────────────────────────────────────
async function loadBatches() {
  await loadDays();
}

function renderBatches(days) {
  const wrap = $("batch-list");
  if (!days.length) {
    wrap.innerHTML = "<div class='loading'>No batches. Create <span class='mono'>inbox/{day_id}/</span> and upload PDFs.</div>";
    return;
  }
  wrap.innerHTML = "";
  days.forEach((d) => {
    const c = d.counts || {};
    const card = document.createElement("div");
    card.className = "batch-card";

    const lifecycleBadge = `<span class="badge ${esc(d.lifecycle)}">${esc(d.lifecycle.replace("_", " "))}</span>`;
    const errBadge = d.has_errors ? `<span class="badge err">errors</span>` : "";
    const gateNote = d.gate
      ? `<span>gate <b>${d.gate.n_annotated}/${d.gate.n_sampled}</b></span>` : "";

    card.innerHTML = `
      <span class="day mono">${esc(d.day_id)}</span>
      ${lifecycleBadge} ${errBadge}
      <span class="counts">
        <span>inbox <b>${d.n_inbox}</b></span>
        <span>tracked <b>${d.n_files}</b></span>
        <span>predicted <b>${c.n_predicted ?? 0}</b></span>
        <span>delivered <b>${c.n_delivered ?? 0}</b></span>
        <span>errors <b>${c.n_error ?? 0}</b></span>
        ${gateNote}
      </span>
      <span class="spacer"></span>
      <span class="batch-actions"></span>`;

    const actions = card.querySelector(".batch-actions");
    const btn = (label, cls, fn) => {
      const b = document.createElement("button");
      b.className = `btn tiny ${cls}`;
      b.textContent = label;
      b.onclick = fn;
      actions.appendChild(b);
    };

    if (["uploaded", "parsing", "unknown"].includes(d.lifecycle) || d.n_inbox > d.n_files)
      btn("▶ Run ingest", "primary", () => openIngest(d.day_id));
    if (d.lifecycle === "awaiting_annotation")
      btn("✎ Annotate", "primary", () => goTab("annotate", d.day_id));
    if (["annotated", "predicted"].includes(d.lifecycle))
      btn("⇪ Split & upload", "primary", () => openDeliver(d.day_id));
    if (d.lifecycle === "delivering")
      btn("⇪ Resume delivery", "", () => openDeliver(d.day_id));
    btn("Live flow", "", () => goTab("flow", d.day_id));

    wrap.appendChild(card);
  });
}

// ── Live flow panel ─────────────────────────────────────────────────────────
// Two rows. The main track is the automated pipeline; the manual lane below it
// carries the files a human has to handle — oversized (>100MB, never parsed)
// and parse failures — and rejoins the track at Split, because a hand-annotated
// file is split and delivered like any other.
const MAIN_STAGES = [
  { key: "inbox",     lbl: "Inbox" },
  { key: "parsed",    lbl: "Parsed" },
  { key: "predicted", lbl: "Predicted" },
  { key: "gate",      lbl: "Ground truth" },
  { key: "split",     lbl: "Split" },
  { key: "delivered", lbl: "Delivered" },
];
// col = which main-track column the lane node sits under.
const LANE_STAGES = [
  { key: "oversized", lbl: "Oversized",    col: 1 },
  { key: "failed",    lbl: "Failed parse", col: 2 },
  { key: "manual",    lbl: "Manual",       col: 4, soft: true },
];

// id, from, to, shape. "h" horizontal · "drop" down into the lane ·
// "join" back up onto the main track.
const WIRES = [
  ["inbox>parsed",     "inbox",     "parsed",    "h"],
  ["parsed>predicted", "parsed",    "predicted", "h"],
  ["predicted>gate",   "predicted", "gate",      "h"],
  ["gate>split",       "gate",      "split",     "h"],
  ["split>delivered",  "split",     "delivered", "h"],
  ["inbox>oversized",  "inbox",     "oversized", "drop"],
  ["parsed>failed",    "parsed",    "failed",    "drop"],
  ["oversized>failed", "oversized", "failed",    "h"],
  ["failed>manual",    "failed",    "manual",    "h"],
  ["manual>split",     "manual",    "split",     "join"],
];

// Flow poll interval. Counts are tweened over roughly this long, so the two
// must stay in step: a tween longer than the poll never reaches its target.
const POLL_FLOW_MS = 4000;

// events[0].stage — written by EventLogger in the notebooks — tells us exactly
// where the pipeline is, so only that segment animates. Before this, every
// connector lit up whenever any job ran, which said nothing at all.
// 'dashboard' is deliberately absent: a human action is not a pipeline phase.
const STAGE_SEGMENTS = {
  parse:        ["inbox>parsed"],
  split:        ["parsed>predicted"],
  check_export: ["predicted>gate"],
  pdf_split:    ["gate>split", "manual>split"],   // manual files are split too
  sftp_upload:  ["split>delivered"],
};
// Lane drops are not tied to a phase: they light only on the polls where their
// own count actually grows, otherwise we are back to "everything blinks".
const GROWTH_SEGMENTS = {
  "inbox>oversized": "n_oversized",
  "parsed>failed":   "n_failed_parse",
};

// ── Funnel DOM: built ONCE ──────────────────────────────────────────────────
// Regenerating innerHTML every poll would restart every animation and throw
// away the tween state, so the numbers could never climb smoothly.
function buildFunnelDom() {
  const el = $("funnel");
  if (el.dataset.built) return;
  const node = (s, cls) =>
    `<div class="stage ${cls}" id="fn-${s.key}"${s.col ? ` style="grid-column:${s.col}"` : ""}>
       <div class="bubble"><span class="count">0</span></div>
       <div class="lbl">${esc(s.lbl)}</div>
       <div class="note" id="fx-${s.key}"></div>
     </div>`;
  el.innerHTML =
    `<svg class="wires" id="funnel-wires"></svg>
     <div class="frow main">${MAIN_STAGES.map((s, i) => node(s, `m${i + 1}`)).join("")}</div>
     <div class="frow lane">${LANE_STAGES.map((s) =>
        node(s, "lane" + (s.soft ? " soft" : ""))).join("")}</div>`;
  el.dataset.built = "1";
}

// ── Connector geometry ──────────────────────────────────────────────────────
// Paths are measured off the real bubble rects, so the diagram survives a
// resize and a different font without hand-tuned coordinates.
let wirePaths = {}, wireTokens = {}, wireWidth = 0;

function bubbleBox(key) {
  const b = document.querySelector(`#fn-${key} .bubble`).getBoundingClientRect();
  const host = $("funnel");
  const d = host.getBoundingClientRect();
  // #funnel scrolls horizontally and the SVG/tokens are absolute children of
  // it, so they live in CONTENT space, not viewport space.
  const x = b.left - d.left + host.scrollLeft;
  const y = b.top - d.top + host.scrollTop;
  return { x, y, w: b.width, h: b.height, cx: x + b.width / 2, cy: y + b.height / 2 };
}

function buildWires() {
  const host = $("funnel"), svg = $("funnel-wires");
  svg.setAttribute("width", host.scrollWidth);
  svg.setAttribute("height", host.scrollHeight);
  svg.innerHTML = "";
  wirePaths = {};

  for (const [id, from, to, shape] of WIRES) {
    const a = bubbleBox(from), b = bubbleBox(to);
    let d;
    if (shape === "h") {
      d = `M ${a.cx + a.w / 2 + 3} ${a.cy} L ${b.cx - b.w / 2 - 3} ${b.cy}`;
    } else if (shape === "drop") {
      d = `M ${a.cx} ${a.y + a.h + 3} L ${a.cx} ${b.y - 3}`;
    } else {
      const x1 = a.cx + a.w / 2 + 3, y1 = a.cy, x2 = b.cx, y2 = b.y + b.h + 3;
      d = `M ${x1} ${y1} C ${x1 + 60} ${y1}, ${x2} ${y1}, ${x2} ${y2}`;
    }
    wirePaths[id] = d;

    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", d);
    p.setAttribute("class", isLaneWire(id) ? "wire lane" : "wire");
    svg.appendChild(p);
  }

  Object.values(wireTokens).flat().forEach((el) => el.remove());
  wireTokens = {};
  for (const [id] of WIRES) {
    wireTokens[id] = [0, 1, 2].map((i) => {
      const el = document.createElement("div");
      el.className = "flow-token";
      el.style.offsetPath = `path('${wirePaths[id]}')`;
      el.style.animationDelay = `${i * 0.5}s`;
      host.appendChild(el);
      return el;
    });
  }
  wireWidth = host.clientWidth;
}

const isLaneWire = (id) =>
  ["oversized", "failed", "manual"].some((k) => id.includes(k));

// A resize invalidates every rect. Redraw, preserving which segments were live
// so an active run doesn't visibly stall just because the window moved.
addEventListener("resize", () => {
  if (state.tab !== "flow" || !$("funnel").dataset.built) return;
  const on = {};
  for (const [id] of WIRES) on[id] = !!(wireTokens[id] || [])[0]?.classList.contains("on");
  buildWires();
  for (const [id] of WIRES) if (on[id]) wireTokens[id].forEach((el) => el.classList.add("on"));
});

// ── Counts that climb instead of jumping ────────────────────────────────────
// The DB counts really do rise during a run (merge_processing_log commits per
// chunk); tweening over the poll interval just removes the stair-step.
const tweens = new Map();

function setCount(key, value, ms) {
  const el = document.querySelector(`#fn-${key} .count`);
  if (!el) return;
  const from = tweens.get(key)?.cur ?? 0;
  if (from === value) return;
  const host = $(`fn-${key}`);
  host.classList.add("bumped");
  setTimeout(() => host.classList.remove("bumped"), 260);

  const prev = tweens.get(key);
  if (prev?.raf) cancelAnimationFrame(prev.raf);
  const t0 = performance.now();
  const st = { cur: from, raf: 0 };
  tweens.set(key, st);

  const frame = (now) => {
    const k = Math.min(1, (now - t0) / ms);
    st.cur = from + (value - from) * (1 - Math.pow(1 - k, 2));
    el.textContent = Math.round(st.cur);
    if (k < 1) st.raf = requestAnimationFrame(frame);
    else { st.cur = value; el.textContent = value; }
  };
  st.raf = requestAnimationFrame(frame);
}

function resetFunnelTweens() {
  tweens.forEach((t) => t.raf && cancelAnimationFrame(t.raf));
  tweens.clear();
  document.querySelectorAll("#funnel .count").forEach((el) => (el.textContent = "0"));
  document.querySelectorAll("#funnel .note").forEach((el) => (el.innerHTML = ""));
}

/** "3 marked · 1 todo" — "" when the bucket is empty entirely.
 *  The verb differs by node because the work differs: on the lane's two source
 *  nodes the action is marking a file manual, on Manual it is drawing its
 *  boundaries. One word for both would hide that. */
function progressNote(done, todo, verb) {
  if (!done && !todo) return "";
  return `<span class="ok">${done} ${verb}</span> · `
       + (todo ? `<span class="warn">${todo} todo</span>` : "0 todo");
}

async function loadFlow() {
  if (!state.dayId) return;
  $("flow-day").textContent = state.dayId;
  buildFunnelDom();
  const res = await jget(`/api/progress?${dayQ()}`);
  if (res.error) return toast(`Progress: ${res.error}`, true);

  const f = res.funnel || {};
  const v = res.volumes || {};
  const g = res.gate || {};
  const n = (x) => parseInt(x || 0, 10);

  const counts = {
    // processing status and delivery status are ORTHOGONAL axes: a file keeps
    // status='done' all through delivery (n_predicted), while its sftp status
    // advances (n_sftp_pending/delivered/…). So a delivering file is already in
    // n_predicted — re-adding the delivery buckets here double-counts it (100
    // files showed 200 while deliver ran). The split/delivered circles live on
    // the delivery axis alone, so they stay as-is.
    inbox: v.inbox ?? 0,
    parsed: n(f.n_parsed) + n(f.n_predicted),
    predicted: n(f.n_predicted),
    gate: g.n_annotated ?? 0,
    split: n(f.n_sftp_pending) + n(f.n_delivered) + n(f.n_sftp_failed) + n(f.n_deferred),
    delivered: n(f.n_delivered),
    // Manual lane. `?? 0` is load-bearing: if the app is redeployed before
    // sql/views.sql (and its PG twin) are applied, SELECT * simply omits these
    // columns and the bubbles would read NaN. Zeros degrade honestly.
    oversized: n(f.n_oversized ?? 0),
    failed: n(f.n_failed_parse ?? 0),
    manual: n(f.n_manual_total ?? 0),
  };

  setErrorBadge(f.n_delivery_blocked);

  const running = (res.active_runs || []).length > 0;
  $("flow-runstate").textContent = running
    ? `⏳ job running: ${res.active_runs.map((r) => `${r.job} (${r.state})`).join(", ")}`
    : "idle — no active job";
  $("poll-dot").classList.toggle("live", running);

  // ── counts, tweened over the poll so they climb ──────────────────────────
  const dur = Math.max(400, POLL_FLOW_MS * 0.9);
  for (const [key, value] of Object.entries(counts)) setCount(key, value, dur);

  // ── one short status line per stage ──────────────────────────────────────
  const note = (key, html) => { const el = $(`fx-${key}`); if (el) el.innerHTML = html; };
  const toAnnotate = (g.n_sampled ?? 0) - (g.n_annotated ?? 0);
  note("parsed", n(f.n_pending) ? `<span class="warn">${n(f.n_pending)} queued</span>` : "");
  // needs_review: both LLMs failed, delivery is blocked until it is approved or
  // a GT JSON exists. Not a lane node — those files parsed fine (status='done'),
  // they are unverified, not broken.
  note("predicted", n(f.n_needs_review)
    ? `<span class="warn">⚠ ${n(f.n_needs_review)} needs review</span>` : "");
  note("gate", g.n_sampled
    ? `${g.n_annotated}/${g.n_sampled} sampled`
      + (toAnnotate > 0 ? ` · <span class="warn">${toAnnotate} todo</span>` : "")
    : "");
  note("split", n(f.n_deferred) ? `<span class="warn">${n(f.n_deferred)} deferred</span>` : "");
  note("delivered", n(f.n_sftp_failed)
    ? `<span class="warn">${n(f.n_sftp_failed)} failed</span>` : "");
  note("oversized", progressNote(counts.oversized - n(f.n_skipped), n(f.n_skipped), "marked"));
  note("failed", progressNote(counts.failed - n(f.n_error), n(f.n_error), "marked"));
  // n_manual_noted, not n_manual_deliverable: the latter also requires
  // status='manual', and a hand UPDATE of the status (20260801) erases it while
  // boundary_source='manual' survives.
  note("manual", progressNote(n(f.n_manual_noted ?? 0),
                              counts.manual - n(f.n_manual_noted ?? 0), "noted"));

  // ── geometry: only when it can actually be measured ──────────────────────
  // The panel is display:none while another tab is up, so every rect would be
  // zero. Draw on the first visible poll and after a real width change.
  const host = $("funnel");
  if (host.clientWidth > 0 && (host.clientWidth !== wireWidth || !wirePaths["inbox>parsed"])) {
    buildWires();
  }

  // ── which segments move ──────────────────────────────────────────────────
  const stage = ((res.events || [])[0] || {}).stage || "";
  const active = new Set(running ? STAGE_SEGMENTS[stage] || [] : []);
  for (const [seg, key] of Object.entries(GROWTH_SEGMENTS)) {
    const now = n(f[key] ?? 0), before = state.lastFunnel ? state.lastFunnel[seg] : null;
    if (before !== null && now > before) active.add(seg);
  }
  for (const [id] of WIRES) {
    (wireTokens[id] || []).forEach((el) => el.classList.toggle("on", active.has(id)));
  }

  state.lastFunnel = Object.assign({}, counts, {
    "inbox>oversized": n(f.n_oversized ?? 0),
    "parsed>failed": n(f.n_failed_parse ?? 0),
  });

  const feed = $("flow-events");
  feed.innerHTML = (res.events || []).map((e) => `
    <div class="event-row ${e.event_type === "error" ? "err" : ""}">
      <span class="ts">${esc(fmtTs(e.event_ts))}</span>
      <span>${esc(e.stage)}</span>
      <span class="etype">${esc(e.event_type)}</span>
      <span class="fname">${esc(e.filename || e.detail || e.error_message || "")}</span>
    </div>`).join("") || "<div class='loading'>No events yet.</div>";
}

// ── Gate panel ──────────────────────────────────────────────────────────────
async function loadGate() {
  if (!state.dayId) return;
  $("gate-day").textContent = state.dayId;
  const g = await jget(`/api/gate?${dayQ()}`);
  if (g.error) return toast(`Gate: ${g.error}`, true);

  const pct = g.n_sampled ? Math.round((g.n_annotated / g.n_sampled) * 100) : 0;
  const m = g.metrics || {};
  const num = (x, d = 2) => (x == null ? "—" : Number(x).toFixed(d));
  const pctf = (x) => (x == null ? "—" : `${Math.round(Number(x) * 100)}%`);

  $("gate-body").innerHTML = `
    <div class="gate-card">
      <div><b>${g.n_annotated}</b> / <b>${g.n_sampled}</b> sampled files annotated
        ${g.complete ? '<span class="badge annotated">gate complete</span>'
                     : '<span class="badge awaiting_annotation">waiting</span>'}</div>
      <div class="gate-bar"><div class="fill" style="width:${pct}%"></div></div>
      ${g.n_sampled === 0 ? "<p class='muted'>No sample yet — run ingest first.</p>" : ""}
      ${g.missing?.length ? `
        <p class="muted">Still to annotate (${g.missing.length}):</p>
        <div class="missing-list">${g.missing.map(esc).join("<br>")}</div>
        <p><button class="btn primary" onclick="goTab('annotate')">✎ Annotate the ${g.missing.length} missing</button></p>` : ""}
      ${g.metrics && m.n_evaluated > 0 ? `
        <h3>Model vs ground truth (sample of ${esc(m.n_evaluated)})</h3>
        <div class="metric-grid">
          <div class="metric"><span class="m-val">${pctf(m.exact_match_rate)}</span><span class="m-lbl">exact match</span></div>
          <div class="metric"><span class="m-val">${pctf(m.multidoc_rate)}</span><span class="m-lbl">multidoc correct</span></div>
          <div class="metric"><span class="m-val">${num(m.avg_precision)}</span><span class="m-lbl">avg precision</span></div>
          <div class="metric"><span class="m-val">${num(m.avg_recall)}</span><span class="m-lbl">avg recall</span></div>
          <div class="metric"><span class="m-val">${num(m.avg_f1)}</span><span class="m-lbl">avg F1</span></div>
          <div class="metric"><span class="m-val">${num(m.avg_f1_tol)}</span><span class="m-lbl">avg F1 (±1)</span></div>
        </div>` : ""}
      ${g.complete ? `
        <p style="margin-top:16px">
          <button class="btn primary" onclick="openDeliver(state.dayId)">⇪ Proceed to split &amp; upload</button>
        </p>` : ""}
    </div>`;
}

// ── Errors panel ────────────────────────────────────────────────────────────
async function loadErrors() {
  const res = await jget(`/api/errors?${dayQ()}`);
  if (res.error) return toast(`Errors: ${res.error}`, true);
  const rows = res.stuck || [];
  // The badge counts only what BLOCKS delivery, which is a subset of the rows
  // shown here (an sftp 'deferred' is stuck but must not block the retry).
  setErrorBadge(rows.filter((r) =>
    ["error", "skipped", "manual"].includes(r.status) && r.boundary_source !== "manual").length);
  if (!rows.length) {
    $("errors-body").innerHTML = "<div class='loading'>✓ Nothing stuck. All documented errors resolved.</div>";
    return;
  }
  $("errors-body").innerHTML = `
    <div class="bulk-bar">
      <button id="err-bulk-manual" class="btn" disabled onclick="markSelectedManual()">✋ Mark selected manual (0)</button>
    </div>
    <div class="tbl-wrap"><table>
      <thead><tr>
        <th><input type="checkbox" id="err-check-all" onclick="toggleAllErrors(this)"></th>
        <th>day</th><th>file</th><th>status</th><th>reason</th><th>actions</th>
      </tr></thead>
      <tbody>${rows.map((r) => `
        <tr>
          <td>${MARK_MANUAL_KINDS.has(errKind(r))
            ? `<input type="checkbox" class="err-check" data-day="${esc(r.day_id)}" data-fn="${esc(r.filename)}" onclick="updateErrBulk()">`
            : ""}</td>
          <td class="mono">${esc(r.day_id)}</td>
          <td class="fname">${esc(r.filename)}</td>
          <td>${esc(r.status)}${r.sftp_delivery_status ? " / " + esc(r.sftp_delivery_status) : ""}</td>
          <td class="reason">${esc(r.stuck_reason || r.error_message || "")}</td>
          <td>${errActions(r)}</td>
        </tr>`).join("")}
      </tbody></table></div>`;
}

function errChecks() {
  return Array.from(document.querySelectorAll(".err-check"));
}

function toggleAllErrors(master) {
  errChecks().forEach((c) => (c.checked = master.checked));
  updateErrBulk();
}

function updateErrBulk() {
  const sel = errChecks().filter((c) => c.checked);
  const btn = $("err-bulk-manual");
  if (btn) {
    btn.textContent = `✋ Mark selected manual (${sel.length})`;
    btn.disabled = sel.length === 0;
  }
  const master = $("err-check-all");
  if (master) master.checked = sel.length > 0 && sel.length === errChecks().length;
}

async function markSelectedManual() {
  const sel = errChecks().filter((c) => c.checked);
  if (!sel.length) return;
  if (!confirm(`Mark ${sel.length} file(s) as manual?`)) return;
  // Group by day_id (the tab is normally day-scoped, but stay correct if not).
  const byDay = {};
  sel.forEach((c) => {
    (byDay[c.dataset.day] ||= []).push(c.dataset.fn);
  });
  let total = 0;
  for (const [day, filenames] of Object.entries(byDay)) {
    const res = await jpost("/api/mark-manual", { day_id: day, filenames });
    if (res.error) return toast(res.error, true);
    total += res.count || 0;
  }
  toast(`marked ${total} manual`);
  refreshTab();
}
window.toggleAllErrors = toggleAllErrors;
window.updateErrBulk = updateErrBulk;
window.markSelectedManual = markSelectedManual;

// ── Errors: which action does THIS kind of stuck deserve? ───────────────────
// The buttons used to be independent conditions rendered side by side, so a row
// showed every one that happened to match. On a failed physical split that put
// `↻ split PDF` next to `↻ sftp`, and the second one is a trap: retry_sftp sets
// sftp_delivery_status='pending', which is non-NULL, and nb_pdf_split's
// candidate query excludes everything non-NULL — the file never gets split
// again and the upload keeps looking for output PDFs that were never produced.
// The classification now comes from v_stuck_files.stuck_kind, computed by the
// same CASE ladder that writes stuck_reason, so label and action cannot drift.

// Fallback for when stuck_kind is absent: the app deployed ahead of the views,
// or run_local. Mirrors the SQL ladder arm for arm. The time-based arms need
// only the status here — v_stuck_files' WHERE already guarantees a row with
// status='parsing' is there *because* it has been parsing > 2h.
function errKind(r) {
  if (r.stuck_kind) return r.stuck_kind;
  const err = `${r.sftp_delivery_error || ""} ${r.stuck_reason || ""}`;
  if (r.status === "error" && r.error_stage === "parsing") return "parse_error";
  if (r.status === "error" && r.error_stage === "pdf_split") return "split_error";
  if (r.status === "error") return "error_other";
  if (r.status === "skipped") return "oversized";
  if (r.sftp_delivery_status === "failed" && /pdf_split failed:/.test(err)) return "pdf_split_failed";
  if (r.sftp_delivery_status === "failed") return "sftp_failed";
  if (r.sftp_delivery_status === "deferred") return "sftp_deferred";
  if (r.status === "parsing") return "stuck_parsing";
  if (r.status === "parsed") return "stuck_parsed";
  // not_archived and sftp_stale differ only in wording; same (absent) action.
  if (r.sftp_delivery_status === "pending") return "sftp_stale";
  if (r.status === "manual" && r.boundary_source !== "manual") return "awaiting_manual";
  if (r.needs_review === true || r.needs_review === "true") return "needs_review";
  if (r.status === "pending") return "stuck_pending";
  return "error_other";
}

// kind → what an operator can actually do about it. `✋ manual` appears only
// where mark_manual changes something: never on an sftp failure (the file is
// already split — flipping a 'done' file to manual puts it in the manual
// worklist, where a save DELETEs its real split_results row), and never on a
// file that is already status='manual'.
const ERR_ACTIONS = {
  parse_error:      (r) => actBtn("retry-parse", r, "↻ parse") + actBtn("mark-manual", r, "✋ manual"),
  split_error:      (r) => actBtn("retry-split", r, "↻ split") + actBtn("mark-manual", r, "✋ manual"),
  error_other:      (r) => actBtn("mark-manual", r, "✋ manual"),
  oversized:        (r) => actBtn("mark-manual", r, "✋ manual"),
  pdf_split_failed: (r) => actBtn("retry-pdf-split", r, "↻ split PDF"),
  sftp_failed:      (r) => actBtn("retry-sftp", r, "↻ sftp"),
  sftp_deferred:    (r) => actBtn("retry-sftp", r, "↻ sftp"),
  stuck_parsing:    (r) => actBtn("mark-manual", r, "✋ manual"),
  stuck_parsed:     (r) => actBtn("retry-split", r, "↻ split") + actBtn("mark-manual", r, "✋ manual"),
  // Nothing to click: every retry action filters on 'failed'/'deferred', so on a
  // 'pending' row they are no-ops. Saying so beats offering a button that lies.
  not_archived:     () => `<span class="no-action">— rilanciare job_deliver</span>`,
  sftp_stale:       () => `<span class="no-action">— rilanciare job_deliver</span>`,
  awaiting_manual:  (r) => annotateBtn(r),
  needs_review:     (r) => actBtn("approve-review", r, "✓ approve") + actBtn("mark-manual", r, "✋ manual"),
  stuck_pending:    (r) => actBtn("mark-manual", r, "✋ manual"),
};

function errActions(r) {
  const fn = ERR_ACTIONS[errKind(r)] || ERR_ACTIONS.error_other;
  return fn(r);
}

// The bulk bar is the second door to mark-manual and must agree with the first:
// a row that gets no `✋ manual` button gets no checkbox either. Otherwise the
// per-row rule is one click away from being bypassed on 200 files at once.
// Keep in sync with ERR_ACTIONS above.
const MARK_MANUAL_KINDS = new Set([
  "parse_error", "split_error", "error_other", "oversized",
  "stuck_parsing", "stuck_parsed", "needs_review", "stuck_pending",
]);

// Already marked manual, boundaries still missing: the fix is to draw them, not
// to mark it manual again. Navigation, not a server action.
function annotateBtn(row) {
  return `<button class="btn tiny" onclick="goAnnotateManual('${esc(row.day_id)}')">✏️ annota</button> `;
}

function goAnnotateManual(dayId) {
  if (!goTab("annotate", dayId)) return;
  if (window.Annotate && window.Annotate.openManual) window.Annotate.openManual();
}
window.goAnnotateManual = goAnnotateManual;

function actBtn(type, row, label) {
  return `<button class="btn tiny" onclick="doAction('${type}','${esc(row.day_id)}','${esc(row.filename)}')">${label}</button> `;
}

async function doAction(type, dayId, filename) {
  if (!confirm(`${type} → ${filename}?`)) return;
  const res = await jpost(`/api/action/${type}`, { day_id: dayId, filename });
  if (res.error) return toast(res.error, true);
  toast(res.message || "done");
  refreshTab();
}
window.doAction = doAction;

// ── SFTP panel ──────────────────────────────────────────────────────────────
async function loadSftp() {
  const res = await jget(`/api/sftp?${dayQ()}`);
  if (res.error) return toast(`SFTP: ${res.error}`, true);
  const folders = res.folders || [];
  const deferred = res.deferred || [];
  state.deferred = deferred;

  const folderTbl = folders.length ? `
    <div class="tbl-wrap"><table>
      <thead><tr><th>day</th><th>folder</th><th>files</th><th>delivered</th>
        <th>pending</th><th>failed</th><th>deferred</th><th>last delivery</th></tr></thead>
      <tbody>${folders.map((r) => `
        <tr>
          <td class="mono">${esc(r.day_id)}</td>
          <td class="mono">${esc(r.folder_id)}</td>
          <td>${esc(r.n_files)}</td>
          <td>${esc(r.n_delivered)}</td>
          <td>${esc(r.n_pending)}</td>
          <td>${r.n_failed > 0 ? `<b style="color:var(--critical)">⚠ ${esc(r.n_failed)}</b>` : "0"}</td>
          <td>${r.n_deferred > 0 ? `<b style="color:var(--serious)">◔ ${esc(r.n_deferred)}</b>` : "0"}</td>
          <td class="mono">${esc(fmtTs(r.last_delivered_at))}</td>
        </tr>`).join("")}
      </tbody></table></div>` : "<div class='loading'>No delivery activity yet.</div>";

  const deferredBlock = deferred.length ? `
    <div class="panel-sub">
      <h3>Not uploaded — remote folder missing (${deferred.length})</h3>
      <p class="muted">These files were skipped because their folder didn't exist on the SFTP.
        Re-deliver them later to a different remote path.</p>
      <div class="tbl-wrap"><table>
        <thead><tr><th>day</th><th>file</th><th>folder</th><th>why</th></tr></thead>
        <tbody>${deferred.map((r) => `
          <tr><td class="mono">${esc(r.day_id)}</td><td class="fname">${esc(r.filename)}</td>
              <td class="mono">${esc(r.folder_id)}</td><td class="reason">${esc(r.sftp_delivery_error || "")}</td></tr>`).join("")}
        </tbody></table></div>
      <p style="margin-top:10px">
        <button class="btn primary" onclick="openRedeliver()">⇪ Re-deliver all to a new folder…</button>
      </p>
    </div>` : "";

  $("sftp-body").innerHTML = folderTbl + deferredBlock;
}

// ── Review panel ────────────────────────────────────────────────────────────
async function loadReview() {
  const res = await jget(`/api/review?${dayQ()}`);
  if (res.error) return toast(`Review: ${res.error}`, true);
  const rows = res.needs_review || [];
  if (!rows.length) {
    $("review-body").innerHTML = "<div class='loading'>✓ Nothing needs review.</div>";
    return;
  }
  $("review-body").innerHTML = `
    <div class="tbl-wrap"><table>
      <thead><tr><th>day</th><th>file</th><th>pages</th><th>source</th><th>prediction</th><th>actions</th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr>
          <td class="mono">${esc(r.day_id)}</td>
          <td class="fname">${esc(r.filename)}</td>
          <td>${esc(r.total_pages)}</td>
          <td>${esc(r.boundary_source)}</td>
          <td class="mono">${esc(r.predicted_starts)}</td>
          <td>
            ${actBtn("approve-review", r, "✓ deliver unsplit")}
            ${actBtn("retry-split", r, "↻ re-split")}
            ${actBtn("mark-manual", r, "✋ manual")}
          </td>
        </tr>`).join("")}
      </tbody></table></div>`;
}

// ── Files panel ─────────────────────────────────────────────────────────────
async function loadFiles() {
  const q = $("file-q").value.trim();
  const status = $("file-status").value;
  const params = new URLSearchParams();
  if (state.dayId) params.set("day_id", state.dayId);
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  const res = await jget(`/api/files?${params}`);
  if (res.error) return toast(`Files: ${res.error}`, true);
  const rows = res.files || [];
  $("files-body").innerHTML = rows.length ? `
    <div class="tbl-wrap"><table>
      <thead><tr><th>day</th><th>file</th><th>status</th><th>sftp</th><th>pages</th>
        <th>docs</th><th>source</th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr class="clickable" onclick="showFile('${esc(r.day_id)}','${esc(r.filename)}')">
          <td class="mono">${esc(r.day_id)}</td>
          <td class="fname">${esc(r.filename)}</td>
          <td>${esc(r.status)}</td>
          <td>${esc(r.sftp_delivery_status || "")}</td>
          <td>${esc(r.n_pages ?? "")}</td>
          <td>${esc(r.n_documents ?? "")}</td>
          <td>${esc(r.boundary_source ?? "")}</td>
        </tr>`).join("")}
      </tbody></table></div>` : "<div class='loading'>No files match.</div>";
  $("file-detail").classList.add("hidden");
}
$("file-search").onclick = loadFiles;
$("file-q").onkeydown = (e) => { if (e.key === "Enter") loadFiles(); };

async function showFile(dayId, filename) {
  const res = await jget(`/api/file/${encodeURIComponent(filename)}?day_id=${encodeURIComponent(dayId)}`);
  if (res.error) return toast(res.error, true);
  const s = res.status || {};
  const wrap = $("file-detail");
  wrap.classList.remove("hidden");
  wrap.innerHTML = `
    <h3>${esc(filename)} <span class="muted">(${esc(dayId)})</span></h3>
    <p>status <b>${esc(s.status)}</b> · sftp <b>${esc(s.sftp_delivery_status || "—")}</b>
       · pages <b>${esc(s.n_pages ?? "—")}</b> · docs <b>${esc(s.n_documents ?? "—")}</b>
       · source <b>${esc(s.boundary_source ?? "—")}</b>
       ${s.needs_review === "true" ? ' · <span class="badge err">needs review</span>' : ""}</p>
    <h4>Event timeline</h4>
    <div class="event-feed">${(res.events || []).map((e) => `
      <div class="event-row ${e.event_type === "error" ? "err" : ""}">
        <span class="ts">${esc(fmtTs(e.event_ts))}</span>
        <span>${esc(e.stage)}</span>
        <span class="etype">${esc(e.event_type)}</span>
        <span class="fname">${esc(e.detail || e.error_message || (e.old_status ? `${e.old_status} → ${e.new_status}` : ""))}</span>
      </div>`).join("") || "<span class='muted'>no events</span>"}</div>
    ${(res.llm_responses || []).length ? `
      <h4>LLM responses</h4>
      ${res.llm_responses.map((l) => `
        <p class="muted">${esc(l.stage)} · ${esc(l.model_used)} ${l.is_fallback === "true" ? "(fallback)" : ""}
           ${l.error_message ? ` · <b style="color:var(--critical)">${esc(l.error_message)}</b>` : ""}</p>
        ${l.raw_response ? `<pre>${esc(l.raw_response)}</pre>` : ""}`).join("")}` : ""}`;
  wrap.scrollIntoView({ behavior: "smooth" });
}
window.showFile = showFile;

// ── Dialogs: ingest / deliver / redeliver ───────────────────────────────────
function openIngest(dayId) {
  $("dlg-ingest-day").textContent = dayId;
  $("dlg-ingest").showModal();
  $("dlg-ingest-go").onclick = async () => {
    const pct = parseFloat($("dlg-sample-pct").value || "10");
    const res = await jpost("/api/run-ingest", { day_id: dayId, sample_pct: pct });
    if (res.error) return toast(res.error, true);
    toast(`job_ingest launched (run ${res.run_id})`);
    goTab("flow", dayId);
  };
}
window.openIngest = openIngest;

function openDeliver(dayId) {
  $("dlg-deliver-day").textContent = dayId;
  $("dlg-deliver").showModal();
  $("dlg-deliver-go").onclick = async () => {
    const remote = $("dlg-sftp-path").value.trim();
    const res = await jpost("/api/run-deliver", { day_id: dayId, sftp_remote_base: remote });
    if (res.error) {
      toast(res.error, true);
      if (res.gate) goTab("gate", dayId);
      return;
    }
    toast(`job_deliver launched (run ${res.run_id})`);
    goTab("flow", dayId);
  };
}
window.openDeliver = openDeliver;

function openRedeliver() {
  $("dlg-redeliver-day").textContent = state.dayId;
  $("dlg-redeliver-list").innerHTML =
    state.deferred.map((d) => esc(d.filename)).join("<br>") || "(none)";
  $("dlg-redeliver").showModal();
  $("dlg-redeliver-go").onclick = async () => {
    const remote = $("dlg-redeliver-path").value.trim();
    const res = await jpost("/api/redeliver", { day_id: state.dayId, sftp_remote_base: remote });
    if (res.error) return toast(res.error, true);
    toast(`re-delivery launched (run ${res.run_id})`);
    document.querySelector('[data-tab="flow"]').click();
  };
}
window.openRedeliver = openRedeliver;

// ── Polling (live flow refreshes while visible; faster when a job runs) ─────
function schedulePoll() {
  clearTimeout(state.pollTimer);
  // Annotate never polls: a background re-render under someone mid-click is a
  // defect, and /api/progress is the heaviest endpoint. Only loadFlow() sets
  // .live, so clear it here or the dot keeps pulsing after polling stopped.
  if (state.tab === "annotate") {
    $("poll-dot").classList.remove("live");
    return;
  }
  const delay = state.tab === "flow" ? POLL_FLOW_MS : 20000;
  state.pollTimer = setTimeout(async () => {
    try {
      if (state.tab === "flow") await loadFlow();
      else if (state.tab === "batches") await loadDays();
    } catch (e) { /* transient poll errors are silent */ }
    schedulePoll();
  }, delay);
}

// ── Boot ────────────────────────────────────────────────────────────────────
(async () => {
  await loadDays(true);
  schedulePoll();
})();
