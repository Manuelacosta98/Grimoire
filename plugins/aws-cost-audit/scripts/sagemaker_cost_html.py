"""Self-contained HTML dashboard for the SageMaker cost deep dive.

Pure stdlib. The collected data is inlined as JSON and every filter recomputes
the whole page client-side, so the file works offline and can be emailed.

Charts follow the dataviz skill: the categorical palette is the validated
reference instance, assigned in fixed slot order and never cycled (a 7th space
folds into "Other"); the neutral grey "Other" sits between the last hue and the
red residual so no two adjacent stack segments fail CVD separation; every chart
carries a legend and a hover tooltip; and the light-mode contrast WARN on
aqua/yellow/magenta is relieved by the full tables, which are always present.
"""

from __future__ import annotations

import json

PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"]

# Raw: the disclosure chevrons use CSS unicode escapes (\25BE), and in a plain
# string Python reads \25 as an octal escape and eats it.
CSS = r"""
:root {
  color-scheme: light;
  --surface-0:#f7f7f5; --surface-1:#fcfcfb; --surface-2:#f0efec;
  --border:#dcdbd6; --border-strong:#c3c2bb;
  --text-1:#0b0b0b; --text-2:#52514e; --text-3:#83827c;
  --accent:#2a78d6; --good:#0e7a4d; --warn:#a86800; --bad:#b3322f;
  --chip:#eceae5;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4; --s6:#008300;
  --other:#8a8985; --residual:#e34948;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface-0:#121211; --surface-1:#1a1a19; --surface-2:#232322;
    --border:#333331; --border-strong:#4a4a47;
    --text-1:#ffffff; --text-2:#c3c2b7; --text-3:#8f8e88;
    --accent:#3987e5; --good:#3ba876; --warn:#d8a441; --bad:#e66767;
    --chip:#2b2b29;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300;
    --other:#8f8e88; --residual:#e66767;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-0:#121211; --surface-1:#1a1a19; --surface-2:#232322;
  --border:#333331; --border-strong:#4a4a47;
  --text-1:#ffffff; --text-2:#c3c2b7; --text-3:#8f8e88;
  --accent:#3987e5; --good:#3ba876; --warn:#d8a441; --bad:#e66767;
  --chip:#2b2b29;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300;
  --other:#8f8e88; --residual:#e66767;
}

* { box-sizing:border-box; }
body {
  margin:0; background:var(--surface-0); color:var(--text-1);
  font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap { max-width:1500px; margin:0 auto; padding:28px 22px 80px; }
h1 { font-size:22px; margin:0 0 4px; letter-spacing:-0.01em; }
h2 { font-size:15px; margin:34px 0 12px; letter-spacing:-0.005em;
     display:flex; align-items:baseline; gap:10px; }
h2 .sub { font-size:12px; font-weight:400; color:var(--text-3); letter-spacing:0; }

/* A section whose own header is the control that opens it. The heading and its
   subtitle read exactly as they do elsewhere; the whole row is the hit target,
   and the chevron is the same vocabulary as the method notes at the foot. */
details.sect { margin:34px 0 0; }
details.sect > summary { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;
  cursor:pointer; list-style:none; padding-bottom:4px; }
details.sect > summary::-webkit-details-marker { display:none; }
details.sect > summary::marker { content:""; }
details.sect > summary h2 { margin:0; }
details.sect > summary:hover h2, details.sect > summary:hover .sub { color:var(--accent); }
details.sect > summary:focus-visible { outline:2px solid var(--accent); outline-offset:3px; }
details.sect > summary .chev { margin-left:auto;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:11px;
  color:var(--text-3); text-transform:uppercase; letter-spacing:.08em; white-space:nowrap; }
details.sect > summary .chev::after { content:"show \25BE"; }
details.sect[open] > summary .chev::after { content:"hide \25B4"; }
details.sect > summary:hover .chev { color:var(--accent); }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
.dim { color:var(--text-3); }
.muted { color:var(--text-2); }

/* Masthead: title on the left, the run's identity on the right, same as the
   Glue dashboard. Which account this is gets read more often than the title. */
.mast { display:flex; flex-wrap:wrap; align-items:flex-end; gap:24px;
        justify-content:space-between; padding-bottom:18px;
        border-bottom:2px solid var(--border-strong); }
.mast h1 { margin:0; }
.mast .sub { margin:6px 0 0; color:var(--text-2); font-size:13px; line-height:1.65; }
.mast .sub b { color:var(--text-1); font-weight:600; }
.acct { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
        font-size:11.5px; color:var(--text-3); text-align:right; line-height:1.7;
        white-space:nowrap; }
.acct b { color:var(--text-1); font-weight:600; }

/* Theme toggle: one icon, two states, remembered. Same control as the Glue page. */
.tt { display:inline-flex; align-items:center; justify-content:center; width:32px;
      height:32px; margin-top:10px; padding:0; cursor:pointer; border-radius:7px;
      background:var(--surface-1); border:1px solid var(--border); color:var(--text-2); }
.tt:hover { border-color:var(--accent); color:var(--accent); }
.tt:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.tt svg { width:17px; height:17px; display:block; }
:root[data-theme="dark"] .tt .ico-moon { display:none; }
:root:not([data-theme="dark"]) .tt .ico-sun { display:none; }

/* Collapsible method notes, in place of a banner nobody finishes reading. */
details.howto { margin:26px 0 0; }
details.howto > summary { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;
  cursor:pointer; list-style:none; border-bottom:2px solid var(--border-strong);
  padding-bottom:10px; }
details.howto > summary::-webkit-details-marker { display:none; }
details.howto > summary::marker { content:""; }
details.howto > summary h2 { margin:0; font-size:19px; font-weight:700;
  letter-spacing:-0.015em; display:block; }
details.howto > summary:hover h2 { color:var(--accent); }
details.howto > summary:focus-visible { outline:2px solid var(--accent); outline-offset:3px; }
details.howto > summary .chev { margin-left:auto;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:11px;
  color:var(--text-3); text-transform:uppercase; letter-spacing:.08em; white-space:nowrap; }
details.howto > summary .chev::after { content:"show \25BE"; }
details.howto[open] > summary .chev::after { content:"hide \25B4"; }
details.howto > summary:hover .chev { color:var(--accent); }
details.howto[open] > summary { margin-bottom:18px; }

details.howto .card { margin-bottom:20px; }
details.howto .card:last-child { margin-bottom:0; }
details.howto .card > h3 { margin:0 0 3px; font-size:14px; font-weight:600;
  letter-spacing:-0.01em; color:var(--text-1); }
details.howto .card > p { margin:0 0 14px; font-size:12.5px; color:var(--text-2);
  max-width:92ch; line-height:1.6; }
details.howto .card > p:last-child { margin-bottom:0; }
details.howto .card > p b { color:var(--text-1); font-weight:600; }
details.howto .card code { background:var(--surface-2); padding:1px 4px;
  border-radius:3px; font-size:11.5px; }
figure { margin:0; }
figcaption { font-size:12px; color:var(--text-3); margin-top:9px; max-width:92ch;
  line-height:1.5; }
figcaption b { color:var(--text-2); font-weight:600; }
.dia { width:100%; height:auto; display:block; max-width:1000px; margin:0 auto;
  color:var(--text-2); }

@media (max-width:640px) { .acct { text-align:left; white-space:normal; } }

.banner {
  margin:0; padding:14px 16px; border-radius:10px;
  background:var(--surface-1); border:1px solid var(--border);
  border-left:3px solid var(--accent);
}
.banner.warnlevel { border-left-color:var(--warn); }
.banner h3 { margin:0 0 6px; font-size:13px; }
.banner p { margin:0 0 6px; color:var(--text-2); font-size:12.5px; max-width:105ch; }
.banner p:last-child { margin-bottom:0; }
.banner code { background:var(--surface-2); padding:1px 4px; border-radius:3px; font-size:11.5px; }

.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
         gap:10px; margin:18px 0 0; }
.tile { background:var(--surface-1); border:1px solid var(--border); border-radius:10px;
        padding:13px 14px; }
.tile .k { font-size:11px; text-transform:uppercase; letter-spacing:0.05em;
           color:var(--text-3); margin-bottom:5px; }
.tile .v { font-size:21px; font-weight:600; letter-spacing:-0.02em; font-variant-numeric:tabular-nums; }
.tile .n { font-size:11.5px; color:var(--text-3); margin-top:3px; }
.tile .v.good { color:var(--good); } .tile .v.warn { color:var(--warn); }
.tile .v.bad { color:var(--bad); }

.filters { display:flex; flex-wrap:wrap; gap:9px; align-items:flex-end;
           background:var(--surface-1); border:1px solid var(--border);
           border-radius:10px; padding:12px 14px; margin:16px 0 0; }
.f { display:flex; flex-direction:column; gap:4px; }
.f label { font-size:10.5px; text-transform:uppercase; letter-spacing:0.05em; color:var(--text-3); }
.f select, .f input {
  background:var(--surface-0); color:var(--text-1); border:1px solid var(--border);
  border-radius:6px; padding:5px 8px; font-size:12.5px; min-width:120px; font-family:inherit;
}
.f input[type=date] { min-width:132px; }
.f select:focus, .f input:focus { outline:2px solid var(--accent); outline-offset:-1px; }
#reset { align-self:flex-end; background:var(--surface-2); border:1px solid var(--border);
         color:var(--text-2); border-radius:6px; padding:6px 11px; font-size:12px; cursor:pointer; }
#reset:hover { color:var(--text-1); border-color:var(--border-strong); }

.card { background:var(--surface-1); border:1px solid var(--border);
        border-radius:10px; padding:16px; }
.legend { display:flex; flex-wrap:wrap; gap:12px; margin:0 0 10px; font-size:12px; }
.legend span { display:inline-flex; align-items:center; gap:6px; color:var(--text-2); }
.legend i { width:10px; height:10px; border-radius:2px; display:inline-block; flex:none; }

.seg { display:inline-flex; border:1px solid var(--border); border-radius:7px;
       overflow:hidden; background:var(--surface-0); }
.seg button { appearance:none; border:0; background:transparent; color:var(--text-2);
              font:inherit; font-size:12px; padding:4px 12px; cursor:pointer; }
.seg button + button { border-left:1px solid var(--border); }
.seg button[aria-pressed="true"] { background:var(--accent); color:#fff; font-weight:600; }
.seg button:hover:not([aria-pressed="true"]) { color:var(--text-1); background:var(--surface-2); }
#chart-note { margin:0 0 10px; padding:8px 11px; border-radius:7px;
              background:var(--surface-2); color:var(--text-2); font-size:12px;
              max-width:110ch; }
#chart-note b { color:var(--text-1); }
.chartbox { overflow-x:auto; }
svg { display:block; }
svg text { fill:var(--text-2); font-size:10.5px; }
svg text.val { fill:var(--text-1); font-size:10.5px; font-variant-numeric:tabular-nums; }
svg .grid { stroke:var(--border); stroke-width:1; }
svg .axis { stroke:var(--border-strong); stroke-width:1; }
svg .seg { stroke:var(--surface-1); stroke-width:2; }
svg .hit { fill:transparent; cursor:pointer; }
svg .hit:hover ~ .halo, svg g.col:hover rect.seg { filter:brightness(1.12); }

.tbl { width:100%; border-collapse:collapse; font-size:12.5px; }
.tbl th, .tbl td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--border); }
.tbl th { color:var(--text-3); font-weight:600; font-size:11px; text-transform:uppercase;
          letter-spacing:0.04em; cursor:pointer; white-space:nowrap; position:sticky; top:0;
          background:var(--surface-1); z-index:1; }
.tbl th:hover { color:var(--text-1); }
.tbl th.num, .tbl td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.tbl tbody tr:hover { background:var(--surface-2); }
.tbl tbody tr.clickable { cursor:pointer; }
.scroll { max-height:520px; overflow:auto; border:1px solid var(--border); border-radius:10px; }
.scroll .tbl th:first-child, .scroll .tbl td:first-child { padding-left:14px; }

.swatch { width:9px; height:9px; border-radius:2px; display:inline-block; margin-right:7px; flex:none; }
.chip { display:inline-block; background:var(--chip); color:var(--text-2);
        border-radius:4px; padding:1px 6px; font-size:11px; margin:1px 3px 1px 0; }
.chip.src { font-family:ui-monospace,Menlo,monospace; font-size:10px; }
.chip.est { color:var(--warn); }
.chip.exact { color:var(--good); }
.tag { font-size:10.5px; padding:1px 5px; border-radius:4px; border:1px solid var(--border); }

.rec { display:flex; gap:11px; padding:12px 14px; border:1px solid var(--border);
       border-radius:9px; background:var(--surface-1); margin-bottom:9px; }
.rec .sev { flex:none; font-size:10px; text-transform:uppercase; letter-spacing:0.05em;
            padding:2px 7px; border-radius:4px; height:min-content; }
.rec .sev.high { background:rgba(179,50,47,.14); color:var(--bad); }
.rec .sev.medium { background:rgba(168,104,0,.14); color:var(--warn); }
.rec .sev.low { background:var(--surface-2); color:var(--text-2); }
.rec h4 { margin:0 0 4px; font-size:13px; }
.rec p { margin:0; color:var(--text-2); font-size:12.5px; max-width:100ch; }

#tip {
  position:fixed; pointer-events:none; opacity:0; transition:opacity .08s;
  background:var(--surface-1); border:1px solid var(--border-strong);
  border-radius:8px; padding:9px 11px; font-size:12px; z-index:50; max-width:340px;
  box-shadow:0 6px 22px rgba(0,0,0,.16);
}
#tip .th { font-weight:600; margin-bottom:5px; }
#tip .r { display:flex; justify-content:space-between; gap:14px; color:var(--text-2); }
#tip .r b { color:var(--text-1); font-weight:600; font-variant-numeric:tabular-nums; }
#tip .r i { width:8px; height:8px; border-radius:2px; display:inline-block; margin-right:5px; }

details.drill { margin-top:8px; }
details.drill > summary { cursor:pointer; color:var(--text-2); font-size:12.5px; padding:4px 0; }
details.drill > summary:hover { color:var(--text-1); }
.empty { color:var(--text-3); font-size:12.5px; padding:20px; text-align:center; }
footer { margin-top:44px; padding-top:16px; border-top:1px solid var(--border);
         color:var(--text-3); font-size:11.5px; }
footer code { background:var(--surface-2); padding:1px 4px; border-radius:3px; }
"""


JS = r"""
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const SLOTS = ['--s1','--s2','--s3','--s4','--s5','--s6'];
const MAXSER = SLOTS.length;
const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const money = (v) => (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString('en-US',
  {minimumFractionDigits:2, maximumFractionDigits:2});
const money0 = (v) => '$' + Math.round(v).toLocaleString('en-US');
const hrs = (v) => v.toLocaleString('en-US',{minimumFractionDigits:1,maximumFractionDigits:1}) + 'h';
const pct = (v) => (v*100).toFixed(1) + '%';
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const base = (p) => String(p||'').split('/').pop();

// ---- space labels ------------------------------------------------------- //
// One person can own a `default-<uuid>` space in several domains, which would
// otherwise render as the same label more than once in the legend. Disambiguate
// collisions with the project, then the domain id.
let LABELS = null;
function buildLabels() {
  const raw = {}, count = {};
  for (const sk of Object.keys(D.spaces)) {
    const m = D.spaces[sk];
    const who = m.owner || (m.owner_uuid ? m.owner_uuid.slice(0,8) : null);
    let name = m.space;
    if (/^default-[0-9a-f-]{36}$/.test(name)) name = who ? ('default (' + who + ')') : name;
    else if (who && !name.includes(who)) name = name + ' · ' + who;
    raw[sk] = name;
    count[name] = (count[name] || 0) + 1;
  }
  LABELS = {};
  for (const sk of Object.keys(raw)) {
    const m = D.spaces[sk];
    LABELS[sk] = count[raw[sk]] > 1
      ? raw[sk] + ' — ' + (m.project || m.domain_name || m.domain_id || sk)
      : raw[sk];
  }
}
function spaceLabel(sk) {
  if (!LABELS) buildLabels();
  return LABELS[sk] || sk;
}
function ownerOf(sk) {
  const m = D.spaces[sk] || {};
  return m.owner || m.owner_uuid || 'unknown';
}
function projectOf(sk) {
  const m = D.spaces[sk] || {};
  return m.project || m.project_id || '—';
}

// ---- filter state ------------------------------------------------------- //
const F = {from:null, to:null, space:'', owner:'', project:'', instance:'', q:''};

function passSD(r) {
  if (F.from && r.day < F.from) return false;
  if (F.to && r.day > F.to) return false;
  if (F.space && r.space_key !== F.space) return false;
  if (F.instance && r.instance !== F.instance) return false;
  if (F.owner && ownerOf(r.space_key) !== F.owner) return false;
  if (F.project && projectOf(r.space_key) !== F.project) return false;
  return true;
}
function passNB(r) {
  if (F.from && r.day < F.from) return false;
  if (F.to && r.day > F.to) return false;
  if (F.space && r.space_key !== F.space) return false;
  if (F.owner && ownerOf(r.space_key) !== F.owner) return false;
  if (F.project && projectOf(r.space_key) !== F.project) return false;
  if (F.q && !r.path.toLowerCase().includes(F.q)) return false;
  return true;
}
function passRecon(r) {
  if (F.from && r.day < F.from) return false;
  if (F.to && r.day > F.to) return false;
  if (F.instance && r.instance !== F.instance) return false;
  return true;
}

// A file filter narrows which space-days are in scope, so the two views agree.
function scopedSpaceDays() {
  let sd = D.space_days.filter(passSD);
  if (F.q) {
    const keep = new Set(D.notebooks.filter(passNB).map(r => r.space_key + '|' + r.day));
    sd = sd.filter(r => keep.has(r.space_key + '|' + r.day));
  }
  return sd;
}

// ---- colour assignment: by entity, fixed order, never by rank ----------- //
// Each mode gets its OWN order, computed once over the unfiltered period, so a
// filter that removes series never repaints the survivors.
const NOFILE = '\u0000no-file-signal';
let COLOR_ORDER = [];      // spaces
let COLOR_ORDER_NB = [];   // notebooks
function buildColorOrder() {
  const bySpace = {}, byNb = {};
  for (const r of D.space_days) bySpace[r.space_key] = (bySpace[r.space_key]||0) + r.cost;
  for (const r of D.notebooks) byNb[r.path] = (byNb[r.path]||0) + r.alloc_cost;
  COLOR_ORDER = Object.keys(bySpace).sort((a,b) => bySpace[b]-bySpace[a]);
  COLOR_ORDER_NB = Object.keys(byNb).sort((a,b) => byNb[b]-byNb[a]);
}
function slotColor(order, key) {
  const i = order.indexOf(key);
  return (i >= 0 && i < MAXSER) ? cssv(SLOTS[i]) : cssv('--other');
}
function colorOf(sk) { return slotColor(COLOR_ORDER, sk); }
function colorOfNb(p) { return slotColor(COLOR_ORDER_NB, p); }

// ---- tooltip ------------------------------------------------------------ //
const tip = $('#tip');
function showTip(evt, html) {
  tip.innerHTML = html;
  tip.style.opacity = 1;
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + w > innerWidth - 8) x = evt.clientX - w - pad;
  if (y + h > innerHeight - 8) y = evt.clientY - h - pad;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function hideTip() { tip.style.opacity = 0; }

// ---- daily stacked bars ------------------------------------------------- //
// One stacked-bar renderer, parameterised by what a series *is*. Space mode and
// Notebook mode differ only in the key, the label, the value and the palette
// order -- every rule below (fixed slots, fold to Other, legend, tooltip,
// click-to-filter) is shared, so the two views cannot drift apart.
function renderStackedDaily(rows, cfg) {
  const host = $('#chart-daily');
  const byDay = {};
  for (const r of rows) {
    const k = cfg.keyOf(r);
    (byDay[r.day] = byDay[r.day] || {})[k] = (byDay[r.day][k]||0) + cfg.valueOf(r);
  }
  const days = Object.keys(byDay).sort();
  if (!days.length) { host.innerHTML = '<div class="empty">' + cfg.emptyText + '</div>';
                      $('#legend-daily').innerHTML = ''; return; }

  const present = new Set(rows.map(cfg.keyOf));
  const residual = cfg.residualKey && present.has(cfg.residualKey);
  const pool = cfg.order.filter(k => k !== cfg.residualKey);
  const named = pool.filter(k => present.has(k)).slice(0, MAXSER);
  const otherKeys = pool.filter(k => present.has(k) && !named.includes(k));
  // Order matters for colourblind separation: hues, then neutral grey Other,
  // then the red residual -- so no two adjacent segments are a failing pair.
  const series = named
    .concat(otherKeys.length ? ['__other__'] : [])
    .concat(residual ? [cfg.residualKey] : []);

  const bw = Math.max(9, Math.min(30, Math.floor(1180 / days.length) - 3));
  const gap = 3, L = 62, R = 14, T = 12, B = 58;
  const W = L + R + days.length * (bw + gap), H = 300;
  const plotH = H - T - B;
  let max = 0;
  for (const d of days) max = Math.max(max, Object.values(byDay[d]).reduce((a,b)=>a+b,0));
  const nice = niceMax(max);
  const y = (v) => T + plotH - (v / nice) * plotH;

  let s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H +
          '" role="img" aria-label="' + cfg.aria + '">';
  for (const t of ticks(nice, 4)) {
    s += '<line class="grid" x1="' + L + '" x2="' + (W-R) + '" y1="' + y(t) + '" y2="' + y(t) + '"/>' +
         '<text x="' + (L-8) + '" y="' + (y(t)+3.5) + '" text-anchor="end">' + money0(t) + '</text>';
  }
  s += '<line class="axis" x1="' + L + '" x2="' + (W-R) + '" y1="' + y(0) + '" y2="' + y(0) + '"/>';

  const labelEvery = Math.ceil(days.length / 18);
  days.forEach((d, di) => {
    const x = L + di * (bw + gap);
    const vals = byDay[d];
    const total = Object.values(vals).reduce((a,b)=>a+b,0);
    const parts = series.map(k => k === '__other__'
      ? {k, v: otherKeys.reduce((a,sk)=>a+(vals[sk]||0),0)}
      : {k, v: vals[k]||0}).filter(p => p.v > 0);

    let acc = 0;
    s += '<g class="col">';
    parts.forEach((p, pi) => {
      const y0 = y(acc + p.v), y1 = y(acc);
      const h = Math.max(1, y1 - y0);
      const top = pi === parts.length - 1;
      const col = p.k === '__other__' ? cssv('--other') : cfg.colorOf(p.k);
      // 4px rounded data-end only on the topmost segment; baseline stays square
      s += '<path class="seg" fill="' + col + '" d="' + roundedTop(x, y0, bw, h, top ? 4 : 0) + '"/>';
      acc += p.v;
    });
    s += '</g>';
    const tipRows = parts.slice().reverse().map(p => {
      const nm = p.k === '__other__'
        ? ('Other (' + otherKeys.length + ' ' + cfg.noun + ')') : cfg.labelOf(p.k);
      const col = p.k === '__other__' ? cssv('--other') : cfg.colorOf(p.k);
      return '<div class="r"><span><i style="background:' + col + '"></i>' + esc(nm) +
             '</span><b>' + money(p.v) + '</b></div>';
    }).join('');
    s += '<rect class="hit" x="' + x + '" y="' + T + '" width="' + bw + '" height="' + plotH +
         '" data-tip="' + esc('<div class="th">' + d + ' · ' + money(total) +
             (cfg.tipNote || '') + '</div>' + tipRows) +
         '" data-day="' + d + '"/>';
    if (di % labelEvery === 0) {
      s += '<text x="' + (x + bw/2) + '" y="' + (H - B + 16) + '" text-anchor="end" ' +
           'transform="rotate(-45 ' + (x + bw/2) + ' ' + (H - B + 16) + ')">' + d.slice(5) + '</text>';
    }
  });
  s += '</svg>';
  host.innerHTML = s;

  host.querySelectorAll('.hit').forEach(el => {
    el.addEventListener('mousemove', e => showTip(e, el.dataset.tip));
    el.addEventListener('mouseleave', hideTip);
    el.addEventListener('click', () => { $('#f-from').value = $('#f-to').value = el.dataset.day; sync(); });
  });

  $('#legend-daily').innerHTML = series.map(k => {
    const nm = k === '__other__'
      ? ('Other (' + otherKeys.length + ' ' + cfg.noun + ')') : cfg.labelOf(k);
    const col = k === '__other__' ? cssv('--other') : cfg.colorOf(k);
    return '<span><i style="background:' + col + '"></i>' + esc(nm) + '</span>';
  }).join('');
}

// ---- the two modes ------------------------------------------------------ //
let chartMode = 'space';
function renderDaily(sd, nb) {
  const note = $('#chart-note');
  if (chartMode === 'notebook') {
    // Some space-days have real cost but no file signal at all. Left out, the
    // notebook view would quietly total less than the space view for the same
    // day. Carry the difference as an explicit residual series so the two modes
    // reconcile and the gap is visible instead of implied.
    const bySpaceDay = {}, byNbDay = {};
    for (const r of sd) bySpaceDay[r.day] = (bySpaceDay[r.day]||0) + r.cost;
    for (const r of nb) byNbDay[r.day] = (byNbDay[r.day]||0) + r.alloc_cost;
    const withResidual = nb.slice();
    for (const d of Object.keys(bySpaceDay)) {
      const gap = bySpaceDay[d] - (byNbDay[d] || 0);
      if (gap > 0.005) {
        withResidual.push({day: d, path: NOFILE, alloc_cost: gap,
                           alloc_hours: 0, space_key: null, tier: null});
      }
    }
    renderStackedDaily(withResidual, {
      order: COLOR_ORDER_NB.concat([NOFILE]),
      keyOf: (r) => r.path,
      valueOf: (r) => r.alloc_cost,
      labelOf: (p) => p === NOFILE ? 'No file signal' : base(p),
      colorOf: (p) => p === NOFILE ? cssv('--residual') : colorOfNb(p),
      noun: 'files',
      residualKey: NOFILE,
      aria: 'Daily SageMaker cost allocated by notebook',
      emptyText: 'No file-level activity in this selection.',
      tipNote: ' · allocated',
    });
    note.hidden = false;
  } else {
    renderStackedDaily(sd, {
      order: COLOR_ORDER,
      keyOf: (r) => r.space_key,
      valueOf: (r) => r.cost,
      labelOf: spaceLabel,
      colorOf: colorOf,
      noun: 'spaces',
      aria: 'Daily SageMaker cost by space',
      emptyText: 'No spend in this selection.',
      tipNote: '',
    });
    note.hidden = true;
  }
}

function roundedTop(x, y, w, h, r) {
  r = Math.min(r, w/2, h);
  if (r <= 0) return 'M' + x + ' ' + y + 'h' + w + 'v' + h + 'h' + (-w) + 'Z';
  return 'M' + x + ' ' + (y + r) + 'a' + r + ' ' + r + ' 0 0 1 ' + r + ' ' + (-r) +
         'h' + (w - 2*r) + 'a' + r + ' ' + r + ' 0 0 1 ' + r + ' ' + r +
         'v' + (h - r) + 'h' + (-w) + 'Z';
}
function niceMax(v) {
  if (v <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1,1.25,1.5,2,2.5,3,4,5,7.5,10]) if (mag*m >= v) return mag*m;
  return mag*10;
}
function ticks(max, n) {
  const out = []; for (let i=1;i<=n;i++) out.push(max*i/n); return out;
}

// ---- top files: horizontal bars, single series -------------------------- //
function renderFiles(nb) {
  const host = $('#chart-files');
  const by = {};
  for (const r of nb) by[r.path] = (by[r.path]||0) + r.alloc_cost;
  const top = Object.entries(by).sort((a,b)=>b[1]-a[1]).slice(0,15);
  if (!top.length) { host.innerHTML = '<div class="empty">No file activity in this selection.</div>'; return; }

  const rowH = 22, L = 300, R = 96, T = 6, W = 1000, H = T + top.length*rowH + 8;
  const max = niceMax(top[0][1]);
  const x = (v) => L + (v/max) * (W - L - R);
  let s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H +
          '" role="img" aria-label="Top files by allocated cost">';
  top.forEach(([path, cost], i) => {
    const yy = T + i*rowH, bh = rowH - 8;
    const w = Math.max(2, x(cost) - L);
    const prof = D.profiles[path] || {};
    const rows =
      '<div class="r"><span>allocated</span><b>' + money(cost) + '</b></div>' +
      (prof.libs && prof.libs.length ? '<div class="r"><span>libs</span><b>' +
        esc(prof.libs.slice(0,5).join(', ')) + '</b></div>' : '') +
      (prof.tables && prof.tables.length ? '<div class="r"><span>reads</span><b>' +
        esc(prof.tables.slice(0,4).join(', ')) + '</b></div>' : '') +
      (prof.cells ? '<div class="r"><span>cells</span><b>' + prof.cells + ' (' +
        prof.code_cells + ' code)</b></div>' : '');
    s += '<text x="' + (L-10) + '" y="' + (yy + bh/2 + 3.5) + '" text-anchor="end">' +
         esc(trunc(path, 46)) + '</text>' +
         '<path fill="' + cssv('--s1') + '" d="' + roundedRight(L, yy, w, bh, 4) + '"/>' +
         '<text class="val" x="' + (x(cost) + 8) + '" y="' + (yy + bh/2 + 3.5) + '">' +
         money(cost) + '</text>' +
         '<rect class="hit" x="' + L + '" y="' + yy + '" width="' + (W-L-R) + '" height="' + rowH +
         '" data-tip="' + esc('<div class="th">' + esc(base(path)) + '</div>' + rows) + '"/>';
  });
  s += '</svg>';
  host.innerHTML = s;
  host.querySelectorAll('.hit').forEach(el => {
    el.addEventListener('mousemove', e => showTip(e, el.dataset.tip));
    el.addEventListener('mouseleave', hideTip);
  });
}
function roundedRight(x, y, w, h, r) {
  r = Math.min(r, w, h/2);
  if (r <= 0) return 'M' + x + ' ' + y + 'h' + w + 'v' + h + 'h' + (-w) + 'Z';
  return 'M' + x + ' ' + y + 'h' + (w - r) + 'a' + r + ' ' + r + ' 0 0 1 ' + r + ' ' + r +
         'v' + (h - 2*r) + 'a' + r + ' ' + r + ' 0 0 1 ' + (-r) + ' ' + r + 'h' + (r - w) + 'Z';
}
function trunc(s, n) { s = String(s); return s.length <= n ? s : '…' + s.slice(-(n-1)); }

// ---- tiles -------------------------------------------------------------- //
function renderTiles(sd, nb, rc) {
  const cost = sd.reduce((a,r)=>a+r.cost,0);
  const hours = sd.reduce((a,r)=>a+r.hours,0);
  const bh = rc.reduce((a,r)=>a+r.billed_hours,0);
  const ah = rc.reduce((a,r)=>a+r.allocated_hours,0);
  const cov = bh ? ah/bh : null;
  const spaces = new Set(sd.map(r=>r.space_key)).size;
  const owners = new Set(sd.map(r=>ownerOf(r.space_key))).size;
  const files = new Set(nb.map(r=>r.path)).size;
  const days = new Set(sd.map(r=>r.day)).size;
  const covClass = cov == null ? '' : (cov < 0.95 ? 'bad' : (cov >= 0.995 ? 'good' : 'warn'));
  const unattr = rc.reduce((a,r)=>a+r.unattributed_cost,0);

  // `hours` is the selection's own priced hours; `bh`/`ah` come from the billed
  // pool for the days and instance types the selection touches. Under an entity
  // filter those are different quantities, so the notes say which is which.
  const scoped = !!(F.space || F.owner || F.project || F.q);
  const tiles = [
    ['Attributed cost', money(cost), days + ' active day' + (days===1?'':'s')],
    ['Priced hours', hrs(hours), scoped ? 'for this selection' : 'partitioned across spaces'],
    ['Priced coverage', cov==null ? '—' : pct(cov),
     cov==null ? 'no billed hours'
               : (unattr > 0.005 ? money(unattr) + ' unattributed'
                                 : 'of billed hours on these days'), covClass],
    ['Spaces', String(spaces), owners + ' owner' + (owners===1?'':'s')],
    ['Files attributed', String(files), 'allocated, not measured'],
    ['Storage', money(D.meta.storage_cost), 'EBS volumes (whole period)'],
  ];
  $('#tiles').innerHTML = tiles.map(([k,v,n,c]) =>
    '<div class="tile"><div class="k">' + k + '</div><div class="v ' + (c||'') + '">' + v +
    '</div><div class="n">' + esc(n) + '</div></div>').join('');
}

// ---- tables ------------------------------------------------------------- //
const sortState = {};
function makeTable(hostId, cols, rows, opts) {
  opts = opts || {};
  const st = sortState[hostId] = sortState[hostId] || {i: opts.sort ?? 0, dir: opts.dir || -1};
  const cmp = (a,b) => {
    const c = cols[st.i], va = c.val(a), vb = c.val(b);
    if (typeof va === 'number' && typeof vb === 'number') return (va-vb)*st.dir;
    return String(va).localeCompare(String(vb)) * st.dir;
  };
  const sorted = rows.slice().sort(cmp);
  const head = cols.map((c,i) =>
    '<th class="' + (c.num?'num':'') + '" data-i="' + i + '">' + c.h +
    (i===st.i ? (st.dir<0?' ↓':' ↑') : '') + '</th>').join('');
  const body = sorted.length
    ? sorted.map(r => '<tr>' + cols.map(c =>
        '<td class="' + (c.num?'num':'') + '">' + c.cell(r) + '</td>').join('') + '</tr>').join('')
    : '<tr><td colspan="' + cols.length + '"><div class="empty">Nothing matches these filters.</div></td></tr>';
  const host = $(hostId);
  host.innerHTML = '<table class="tbl"><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table>';
  host.querySelectorAll('th').forEach(th => th.addEventListener('click', () => {
    const i = +th.dataset.i;
    if (st.i === i) st.dir = -st.dir; else { st.i = i; st.dir = -1; }
    render();
  }));
}

function renderSpaceTable(sd) {
  const agg = {};
  for (const r of sd) {
    const a = agg[r.space_key] = agg[r.space_key] ||
      {sk:r.space_key, cost:0, hours:0, days:new Set(), inst:{}, hsrc:new Set(), tsrc:new Set()};
    a.cost += r.cost; a.hours += r.hours; a.days.add(r.day);
    a.inst[r.instance] = (a.inst[r.instance]||0) + r.hours;
    a.hsrc.add(r.hours_source); a.tsrc.add(r.type_source);
  }
  const rows = Object.values(agg);
  const total = rows.reduce((a,r)=>a+r.cost,0) || 1;
  makeTable('#tbl-spaces', [
    {h:'Space', val:r=>spaceLabel(r.sk), cell:r =>
      '<span class="swatch" style="background:' + colorOf(r.sk) + '"></span>' +
      esc(spaceLabel(r.sk)) +
      (D.spaces[r.sk] && D.spaces[r.sk].exists === false
        ? ' <span class="tag dim" title="No longer present in the SageMaker API">deleted</span>' : '')},
    {h:'Owner', val:r=>ownerOf(r.sk), cell:r=>esc(ownerOf(r.sk))},
    {h:'Project', val:r=>projectOf(r.sk), cell:r=>esc(projectOf(r.sk))},
    {h:'Instances', val:r=>Object.keys(r.inst).join(','), cell:r =>
      Object.entries(r.inst).sort((a,b)=>b[1]-a[1])
        .map(([i,h]) => '<span class="chip mono">' + esc(i||'unknown') + ' ' + hrs(h) + '</span>').join('')},
    {h:'Days', val:r=>r.days.size, cell:r=>r.days.size, num:true},
    {h:'Priced h', val:r=>r.hours, cell:r=>hrs(r.hours), num:true},
    {h:'Cost', val:r=>r.cost, cell:r=>money(r.cost), num:true},
    {h:'% of sel.', val:r=>r.cost/total, cell:r=>pct(r.cost/total), num:true},
    {h:'Source', val:r=>[...r.tsrc].join(','), cell:r =>
      [...r.hsrc].map(s=>'<span class="chip src ' + (s==='cloudtrail'?'exact':'') + '">' + s + '</span>').join('') +
      [...r.tsrc].map(s=>'<span class="chip src ' + (s==='cloudtrail'?'exact':'est') + '">type:' + s + '</span>').join('')},
  ], rows, {sort:6});
}

const TIER_LABEL = {
  'exec-seconds': ['exact-ish', 'cell executions x this notebook\'s own sec/cell'],
  'exec-count':   ['est', 'cell executions x the corpus median sec/cell'],
  'editor-sync':  ['weak', 'editor open/sync events only - measures open, not run'],
};

function renderFileTable(nb) {
  const agg = {};
  for (const r of nb) {
    const a = agg[r.path] = agg[r.path] ||
      {path:r.path, cost:0, hours:0, days:new Set(), spaces:new Set(), act:0,
       execs:0, kh:0, low:false, tiers:new Set(), ident:true};
    a.cost += r.alloc_cost; a.hours += r.alloc_hours; a.days.add(r.day);
    a.spaces.add(r.space_key); a.act += (r.activity || 0);
    a.execs += (r.execs || 0); a.kh += (r.kernel_hours || 0);
    a.tiers.add(r.tier);
    if (r.identified === false) a.ident = false;
    if (r.low_confidence) a.low = true;
  }
  const rows = Object.values(agg);
  makeTable('#tbl-files', [
    {h:'File', val:r=>base(r.path), cell:r =>
      '<span title="' + esc(r.path) + '">' + esc(base(r.path)) + '</span>' +
      (r.low ? ' <span class="tag" style="color:var(--warn)" title="Too little signal that day to carry a ratio, or a spread indistinguishable from an equal split">wide error</span>' : '') +
      (r.ident === false ? ' <span class="tag" style="color:var(--bad)" title="The telemetry hash did not resolve to a known path; its cost is reported here rather than dropped">unidentified</span>' : '') +
      '<div class="dim" style="font-size:11px">' + esc(r.path.split('/').slice(0,-1).join('/')) + '</div>'},
    {h:'Owners', val:r=>[...r.spaces].map(ownerOf).join(','), cell:r =>
      [...new Set([...r.spaces].map(ownerOf))].map(o=>'<span class="chip">' + esc(o) + '</span>').join('')},
    {h:'Days', val:r=>r.days.size, cell:r=>r.days.size, num:true},
    {h:'Cell runs', val:r=>r.execs, cell:r =>
      r.execs ? r.execs.toLocaleString('en-US') : '<span class="dim">—</span>', num:true},
    {h:'Sec/cell', val:r=>{const p=D.profiles[r.path]||{};return p.sec_per_cell||0;}, cell:r => {
      const p = D.profiles[r.path] || {};
      if (!p.sec_per_cell) return '<span class="dim">—</span>';
      return p.sec_per_cell.toFixed(1) +
        '<span class="dim" title="timed cells in the notebook\'s saved metadata"> ·' +
        (p.timed_cells||0) + 'c</span>';
    }, num:true},
    {h:'Kernel h', val:r=>r.kh, cell:r =>
      r.kh ? hrs(r.kh) : '<span class="dim">—</span>', num:true},
    {h:'Basis', val:r=>[...r.tiers].sort().join(','), cell:r =>
      [...r.tiers].map(t => {
        const [lab, why] = TIER_LABEL[t] || [t, ''];
        const cls = t === 'exec-seconds' ? 'exact' : (t === 'editor-sync' ? 'est' : '');
        return '<span class="chip src ' + cls + '" title="' + esc(why) + '">' + lab + '</span>';
      }).join('')},
    {h:'Est. hours', val:r=>r.hours, cell:r=>hrs(r.hours), num:true},
    {h:'Est. cost', val:r=>r.cost, cell:r=>money(r.cost), num:true},
    {h:'Libraries', val:r=>(D.profiles[r.path]||{}).libs?.length||0, cell:r => {
      const p = D.profiles[r.path];
      if (!p) return '<span class="dim">not matched in S3</span>';
      if (p.error) return '<span class="dim" title="' + esc(p.error) + '">' + esc(p.error.slice(0,40)) + '</span>';
      return (p.libs||[]).slice(0,6).map(l=>'<span class="chip">' + esc(l) + '</span>').join('') || '<span class="dim">—</span>';
    }},
    {h:'Reads', val:r=>((D.profiles[r.path]||{}).tables||[]).join(','), cell:r => {
      const p = D.profiles[r.path] || {};
      return (p.tables||[]).slice(0,4).map(t=>'<span class="chip mono">' + esc(t) + '</span>').join('') || '<span class="dim">—</span>';
    }},
    {h:'Cells', val:r=>(D.profiles[r.path]||{}).cells||0, cell:r => {
      const p = D.profiles[r.path] || {};
      return p.cells ? (p.cells + ' <span class="dim">(' + p.code_cells + ' code)</span>') : '<span class="dim">—</span>';
    }, num:true},
    {h:'Modified', val:r=>(D.profiles[r.path]||{}).last_modified||'', cell:r => {
      const p = D.profiles[r.path] || {};
      return p.last_modified ? '<span class="mono dim">' + p.last_modified.slice(0,10) + '</span>' : '<span class="dim">—</span>';
    }},
  ], rows, {sort:8});
}

function renderRecon(rc) {
  makeTable('#tbl-recon', [
    {h:'Day', val:r=>r.day, cell:r=>'<span class="mono">' + r.day + '</span>'},
    {h:'Instance', val:r=>r.instance, cell:r=>'<span class="mono">' + esc(r.instance) + '</span>'},
    {h:'Rate/h', val:r=>r.rate, cell:r=>money(r.rate), num:true},
    {h:'Billed h', val:r=>r.billed_hours, cell:r=>hrs(r.billed_hours), num:true},
    {h:'Priced h', val:r=>r.allocated_hours, cell:r=>hrs(r.allocated_hours), num:true},
    {h:'Priced', val:r=>r.coverage==null?-1:r.coverage, cell:r => {
      if (r.coverage == null) return '<span class="dim">—</span>';
      const c = r.coverage < 0.95 ? 'var(--bad)' : (r.coverage >= 0.995 ? 'var(--good)' : 'var(--warn)');
      return '<span style="color:' + c + '">' + pct(r.coverage) + '</span>';
    }, num:true},
    {h:'Observed h (day)', val:r=>r.observed_hours, cell:r =>
      '<span class="dim">' + hrs(r.observed_hours) + '</span>', num:true},
    {h:'Spaces', val:r=>r.spaces, cell:r=>r.spaces, num:true},
    {h:'Billed cost', val:r=>r.billed_cost, cell:r=>money(r.billed_cost), num:true},
    {h:'Unattributed', val:r=>r.unattributed_cost, cell:r =>
      r.unattributed_cost > 0.005
        ? '<span style="color:var(--bad)">' + money(r.unattributed_cost) + '</span>'
        : '<span class="dim">—</span>', num:true},
  ], rc, {sort:0, dir:1});
}

function renderDrill(sd, nb) {
  const byDay = {};
  for (const r of sd) (byDay[r.day] = byDay[r.day] || []).push(r);
  const nbByKey = {};
  for (const r of nb) (nbByKey[r.space_key + '|' + r.day] = nbByKey[r.space_key + '|' + r.day] || []).push(r);
  const days = Object.keys(byDay).sort().reverse();
  if (!days.length) { $('#drill').innerHTML = '<div class="empty">Nothing matches these filters.</div>'; return; }

  $('#drill').innerHTML = days.slice(0, 120).map(day => {
    const rows = byDay[day];
    const tot = rows.reduce((a,r)=>a+r.cost,0);
    const inner = rows.sort((a,b)=>b.cost-a.cost).map(r => {
      const files = (nbByKey[r.space_key + '|' + r.day] || []).sort((a,b)=>b.alloc_cost-a.alloc_cost);
      const flist = files.length
        ? '<table class="tbl" style="margin:4px 0 10px">' + files.map(f =>
            '<tr><td style="padding-left:34px">' + esc(base(f.path)) +
            '<span class="dim" style="font-size:11px"> · ' + esc(f.path.split('/').slice(0,-1).join('/')) + '</span></td>' +
            '<td class="num dim">' + f.activity + ' events</td>' +
            '<td class="num dim">' + pct(f.share) + '</td>' +
            '<td class="num">~' + money(f.alloc_cost) + '</td></tr>').join('') + '</table>'
        : '<div class="dim" style="padding:4px 0 10px 34px;font-size:12px">' +
          'No file activity logged for this space-day — the cost is real, the attribution stops at the space.</div>';
      return '<div style="padding-left:18px">' +
        '<div style="display:flex;gap:10px;align-items:baseline;padding:4px 0">' +
        '<span class="swatch" style="background:' + colorOf(r.space_key) + '"></span>' +
        '<b>' + esc(spaceLabel(r.space_key)) + '</b>' +
        '<span class="mono dim">' + esc(r.instance) + '</span>' +
        '<span class="dim">' + hrs(r.hours) + '</span>' +
        '<b>' + money(r.cost) + '</b>' +
        '<span class="chip src ' + (r.hours_source==='cloudtrail'?'exact':'') + '">' + r.hours_source + '</span>' +
        '</div>' + flist + '</div>';
    }).join('');
    return '<details class="drill"><summary><span class="mono">' + day + '</span> · ' +
      money(tot) + ' · ' + rows.length + ' space' + (rows.length===1?'':'s') +
      '</summary>' + inner + '</details>';
  }).join('') + (days.length > 120
    ? '<div class="dim" style="padding:10px 0">Showing the 120 most recent days of ' + days.length + '.</div>'
    : '');
}

// ---- orchestration ------------------------------------------------------ //
function render() {
  const sd = scopedSpaceDays();
  const nb = D.notebooks.filter(passNB).filter(r => !F.instance ||
    sd.some(s => s.space_key === r.space_key && s.day === r.day));
  const rc = D.recon.filter(passRecon).filter(r => {
    if (!(F.space || F.owner || F.project || F.q)) return true;
    return sd.some(s => s.day === r.day && s.instance === r.instance);
  });
  renderTiles(sd, nb, rc);
  renderDaily(sd, nb);
  renderFiles(nb);
  renderSpaceTable(sd);
  renderFileTable(nb);
  renderRecon(rc);
  renderDrill(sd, nb);
}

function sync() {
  F.from = $('#f-from').value || null;
  F.to = $('#f-to').value || null;
  F.space = $('#f-space').value;
  F.owner = $('#f-owner').value;
  F.project = $('#f-project').value;
  F.instance = $('#f-instance').value;
  F.q = $('#f-q').value.trim().toLowerCase();
  render();
}

function initFilters() {
  const opts = (sel, vals, label) => {
    $(sel).innerHTML = '<option value="">' + label + '</option>' +
      vals.map(v => '<option value="' + esc(v) + '">' + esc(v) + '</option>').join('');
  };
  const uniq = (a) => [...new Set(a)].filter(Boolean).sort();
  opts('#f-space', COLOR_ORDER.map(sk => sk), 'All spaces');
  $('#f-space').innerHTML = '<option value="">All spaces</option>' +
    COLOR_ORDER.map(sk => '<option value="' + esc(sk) + '">' + esc(spaceLabel(sk)) + '</option>').join('');
  opts('#f-owner', uniq(Object.keys(D.spaces).map(ownerOf)), 'All owners');
  opts('#f-project', uniq(Object.keys(D.spaces).map(projectOf)), 'All projects');
  opts('#f-instance', uniq(D.space_days.map(r => r.instance)), 'All instances');
  $('#f-from').min = $('#f-to').min = D.meta.start;
  $('#f-from').max = $('#f-to').max = D.meta.end;
  ['#f-from','#f-to','#f-space','#f-owner','#f-project','#f-instance']
    .forEach(s => $(s).addEventListener('change', sync));
  $('#f-q').addEventListener('input', sync);
  $('#reset').addEventListener('click', () => {
    ['#f-from','#f-to','#f-q'].forEach(s => $(s).value = '');
    ['#f-space','#f-owner','#f-project','#f-instance'].forEach(s => $(s).value = '');
    sync();
  });
}

function initChartMode() {
  const btns = {space: $('#mode-space'), notebook: $('#mode-notebook')};
  const apply = (m) => {
    chartMode = m;
    for (const k of Object.keys(btns)) btns[k].setAttribute('aria-pressed', String(k === m));
    render();
  };
  btns.space.addEventListener('click', () => apply('space'));
  btns.notebook.addEventListener('click', () => apply('notebook'));
}

/* ---- theme toggle: light unless the reader has chosen otherwise ---- */
function initTheme() {
  const root = document.documentElement;
  const btn = $('#themeBtn');
  const paint = (t) => {
    const next = t === 'dark' ? 'light' : 'dark';
    btn.setAttribute('aria-label', 'Switch to ' + next + ' theme');
    btn.title = 'Switch to ' + next + ' theme';
  };
  paint(root.getAttribute('data-theme') || 'light');
  btn.addEventListener('click', () => {
    const t = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', t);
    try { localStorage.setItem('sagemaker-theme', t); } catch (e) {}
    paint(t);
    render();   // the charts read their colours from the computed style
  });
}

buildColorOrder();
buildLabels();
initFilters();
initChartMode();
initTheme();
render();
"""


def _file_method(meta: dict) -> str:
    """How the per-file split was actually derived on THIS run, in dollars.

    The tier mix varies by period -- telemetry only exists from 2026-01 and only
    for spaces running a recent Studio image -- so stating the mix is the only
    way a reader can tell how much of the file view to trust.
    """
    tiers = meta.get("weight_tiers") or {}
    total = sum(tiers.values())
    ex = meta.get("executions") or {}
    hr = meta.get("hash_resolution") or {}

    parts = ["<p><b>Per-file cost is an allocation, not a measurement.</b> "]
    if ex.get("executions"):
        parts.append(
            f"Studio logs a <code>jl-cell-executed</code> event for every cell run, so this "
            f"run knows about <b>{ex['executions']:,} real cell executions</b> across "
            f"{ex.get('hashes', 0)} notebooks &mdash; that a notebook <i>ran</i>, not merely "
            f"that it was open. ")
        if hr.get("exec_rate") is not None:
            parts.append(
                f"Each event names its notebook only as an MD5 of the file path; "
                f"<b>{hr['exec_rate']:.1%}</b> of executions were matched back to a real path")
            if hr.get("unresolved_hashes"):
                parts.append(
                    f", and the {hr['unresolved_hashes']} that "
                    f"{'was' if hr['unresolved_hashes'] == 1 else 'were'} not "
                    f"appear{'s' if hr['unresolved_hashes'] == 1 else ''} as "
                    f"<i>unidentified</i> row"
                    f"{'' if hr['unresolved_hashes'] == 1 else 's'} keeping their cost "
                    f"rather than being dropped")
            parts.append(". ")
    else:
        parts.append("No cell-execution telemetry was available for this period, so the split "
                     "falls back to editor open/sync events throughout. ")

    if total > 0:
        mix = []
        for tier, label in (("exec-seconds", "executions &times; that notebook's own sec/cell"),
                            ("exec-count", "executions &times; the corpus median sec/cell"),
                            ("editor-sync", "editor open/sync events only")):
            v = tiers.get(tier) or 0.0
            if v > 0:
                mix.append(f"<b>{100 * v / total:.0f}%</b> by {label}")
        parts.append("Weighting on this run: " + "; ".join(mix) + ". ")

    if meta.get("median_sec_per_cell"):
        parts.append(
            f"Seconds-per-cell comes from each notebook's own saved execution metadata "
            f"(corpus median {meta['median_sec_per_cell']:.1f} s). That metadata records only "
            f"each cell's <i>most recent</i> run, so it is a profile of the notebook rather "
            f"than a measurement of any one day &mdash; a notebook whose runtime changed "
            f"mid-period is priced with its latest profile throughout. ")

    parts.append("Treat the file ranking as reliable and any single figure as an estimate.")
    parts.append(_soft_note(meta))
    parts.append("</p>")
    return "".join(parts)


def _soft_note(meta: dict) -> str:
    """How much of the file-level money rests on a split that carries no real
    information. Without this a reader has to scan the table to find out."""
    total = meta.get("file_alloc_cost") or 0.0
    soft = meta.get("file_alloc_soft_cost") or 0.0
    if total <= 0:
        return ""
    share = soft / total
    return (f" On this run <b>{share * 100:.0f}%</b> of the ${total:,.2f} allocated to files "
            f"(${soft:,.2f}) rests on a split flagged <i>wide error</i> &mdash; too few logged "
            f"events that day, or events spread so evenly across the open files that the ratio "
            f"is indistinguishable from dividing the day equally. Those rows rank; they do not "
            f"measure.")


def _gaps(meta: dict) -> list[str]:
    """What this particular run could not see. Paragraphs, or an empty list."""
    ct = meta.get("cloudtrail") or {}
    occ = meta.get("occupancy") or {}
    out = []

    src = []
    if ct.get("available"):
        detail = f"CloudTrail <code>CreateApp</code>/<code>DeleteApp</code> from {ct.get('from')}"
        if ct.get("clamped"):
            detail += " (Event history reaches no further back)"
        src.append(detail + " &mdash; exact spans and exact instance type")
    if occ.get("streams"):
        src.append(f"CloudWatch Logs Insights occupancy over "
                   f"{len(occ['streams'])} app log streams &mdash; full history, "
                   f"instance type inferred")
    if src:
        out.append("<p><b>Sources:</b> " + "; ".join(src) + ".</p>")

    if ct.get("complete") is False:
        out.append(
            "<p><b>CloudTrail sweep incomplete:</b> at least one day-sweep failed, so its "
            "spans may be missing a <code>DeleteApp</code>. Log occupancy was used for hours "
            "instead, because an app left open over-attributes far more than a quiet log gap "
            "under-attributes.</p>")

    if not ct.get("trail_archive"):
        out.append(
            "<p><b>Gap:</b> no CloudTrail trail or event data store exists, so exact spans "
            "stop at the Event-history horizon. Everything older is reconstructed from log "
            "occupancy, which cannot observe the instance type directly &mdash; those rows "
            "carry <code>type:rank-match</code> rather than <code>type:cloudtrail</code> in "
            "the Source column.</p>")

    if meta.get("unvalidated_kinds"):
        out.append(
            "<p><b>Unvalidated usage families present:</b> <code>"
            + "</code>, <code>".join(meta["unvalidated_kinds"])
            + "</code>. Their attribution strategy has not been reconciled against a bill; "
              "treat their rows as indicative.</p>")

    if meta.get("unattributed_cost", 0) > 0.005:
        out.append(
            f"<p><b>Unattributed:</b> ${meta['unattributed_cost']:,.2f} of runtime cost had no "
            f"space activity in any source. It is real spend the reconstruction cannot place; "
            f"it appears in the reconciliation table and is excluded from space and file rows.</p>")

    for w in meta.get("warnings", []):
        out.append(f'<p><b>Warning:</b> {w}</p>')

    return out


def _card(title: str, paragraphs: list[str]) -> str:
    body = "".join(paragraphs)
    return f'<div class="card"><h3>{title}</h3>{body}</div>'


def _method_cards(meta: dict) -> str:
    """The method, one card per question a reader actually asks of it."""
    priced = meta.get("priced_coverage")
    obs = meta.get("observed_coverage")
    occ = meta.get("occupancy") or {}
    priced_txt = ("no billed runtime hours in this period" if priced is None
                  else f"<b>{priced * 100:.1f}%</b> of billed runtime hours "
                       f"({meta['allocated_hours']:,.1f}h of {meta['billed_hours']:,.1f}h)")
    obs_txt = ("&mdash;" if obs is None
               else f"<b>{obs * 100:.1f}%</b> ({meta['observed_hours']:,.1f}h observed "
                    f"against {meta['billed_hours']:,.1f}h billed)")

    cards = [
        _card("Where the SageMaker spend goes", [
            "<p><b>Dollars are exact.</b> Every figure is a share of the amount AWS actually "
            "billed, distributed across the spaces active on that day pro-rata by observed "
            "runtime. Daily and period totals therefore reconcile to the bill by construction; "
            "no cost is ever computed as hours &times; rate.</p>",
            "<p><b>A day's billed hours are partitioned, not estimated.</b> Each instance "
            "type's billed hours for a day are divided among the spaces active that day: "
            "CloudTrail supplies the type directly where it reaches, and whatever is left is "
            "rank-matched (the space with the most remaining hours draws from the type with "
            "the most remaining hours). A space is not assumed to sit on one instance type all "
            "day &mdash; apps get recreated on bigger instances mid-afternoon. Every row "
            "records its basis in the Source column.</p>",
        ]),
        f'<div class="card"><h3>How the attribution works, and why it reconciles</h3>'
        f"<p>Cost Explorer stops at <code>SERVICE</code> and <code>USAGE_TYPE</code>: it knows "
        f"what SageMaker cost, not whose work spent it. Two independent sources reconstruct the "
        f"layer below, and the bill stays the arbiter of the total.</p>"
        + _diagram(meta) + "</div>",
        _card("Two numbers, kept apart", [
            f"<p><b>Priced coverage:</b> {priced_txt}. A shortfall here is real spend with no "
            f"observed space activity at all &mdash; it is listed in the reconciliation table "
            f"and excluded from space and file rows rather than spread around. The target "
            f"is 98%.</p>",
            f"<p><b>The method's error bar is observed runtime against billed runtime:</b> "
            f"{obs_txt}. Slightly above 100% is expected and benign &mdash; billing starts when "
            f"an app reaches <code>InService</code> while <code>CreateApp</code> fires a minute "
            f"or two earlier, and each occupancy cluster counts its trailing "
            f"{occ.get('bin_minutes', 5)}-minute bin whole. It has not been tuned to reach "
            f"100%. Above about 112%, something is double-counted.</p>",
        ]),
        _card("Splitting a space-day across notebooks", [_file_method(meta)]),
    ]

    gaps = _gaps(meta)
    if gaps:
        cards.append(_card("Sources, gaps and warnings", gaps))
    return "".join(cards)


def _recommendations(recs: list[dict]) -> str:
    if not recs:
        return '<div class="empty">No configuration gaps found.</div>'
    out = []
    for r in recs:
        out.append(
            f'<div class="rec"><span class="sev {r["severity"]}">{r["severity"]}</span>'
            f'<div><h4>{r["title"]}</h4><p>{r["body"]}</p></div></div>')
    return "".join(out)


def _payer(payer: dict) -> str:
    if not payer.get("available"):
        return ('<p class="muted">Ran without a payer profile, so cost-allocation tag status, '
                'CUR availability and resource-level granularity were not checked. Pass '
                '<code>--payer-profile</code> to include them.</p>')
    rows = []
    for k, v in (payer.get("tags") or {}).items():
        colour = "var(--good)" if v == "Active" else "var(--warn)"
        rows.append(f'<tr><td class="mono">{k}</td>'
                    f'<td style="color:{colour}">{v or "not present"}</td></tr>')
    tag_tbl = ("<table class='tbl'><thead><tr><th>Tag</th><th>Status</th></tr></thead><tbody>"
               + "".join(rows) + "</tbody></table>") if rows else \
        "<p class='muted'>No DataZone tags found in the payer's tag list.</p>"
    cur = ", ".join(payer.get("cur") or []) or "none"
    exp = ", ".join(str(e) for e in (payer.get("exports") or [])) or "none"
    return (
        f'<p class="muted">Payer account <span class="mono">{payer.get("account")}</span> &middot; '
        f'{payer.get("tag_total", 0)} cost-allocation tags defined, '
        f'{len(payer.get("tags_active") or [])} active &middot; '
        f'CUR: {cur} &middot; data exports: {exp} &middot; '
        f'resource-level Cost Explorer: '
        f'{"enabled" if payer.get("resource_level") else "not enabled"}</p>' + tag_tbl)


def _diagram(meta: dict) -> str:
    """The attribution path, drawn. Two observation sources become occupancy spans;
    occupancy and the bill together partition each day; the day's cost then splits
    across the notebooks that ran in it."""
    ct = meta.get("cloudtrail") or {}
    occ = meta.get("occupancy") or {}
    tiers = meta.get("weight_tiers") or {}
    tier_total = sum(tiers.values()) or 1.0

    billed = meta.get("billed_hours") or 0.0
    allocated = meta.get("allocated_hours") or 0.0
    observed = meta.get("observed_hours") or 0.0
    priced = meta.get("priced_coverage")
    obs = meta.get("observed_coverage")
    runtime = meta.get("runtime_cost") or 0.0
    to_files = meta.get("file_alloc_cost") or 0.0
    unattributed = meta.get("unattributed_cost") or 0.0
    # Whatever the bill paid for that no notebook can be tied to. It is the
    # remainder of two numbers already on the page, not a separate measurement.
    no_signal = max(runtime - to_files - unattributed, 0.0)

    pct = lambda v: "&mdash;" if v is None else f"{v * 100:.1f}%"
    tier = lambda k: 100 * (tiers.get(k) or 0.0) / tier_total
    ct_line = (f"from {ct.get('from')}" if ct.get("available") and ct.get("from")
               else "not available")
    streams = len(occ.get("streams") or [])

    label = (f"Diagram: CloudTrail app lifecycle events and CloudWatch Logs Insights "
             f"occupancy produce {observed:,.1f} observed hours per space-day; those spans "
             f"and the {billed:,.1f} billed hours from Cost Explorer partition each day "
             f"across the spaces active in it, pricing {allocated:,.1f} hours "
             f"({pct(priced)} coverage); each space-day's cost is then split across the "
             f"notebooks that ran, ${to_files:,.2f} to files and ${no_signal:,.2f} with no "
             f"file signal.")

    return f"""<figure>
      <svg class="dia" viewBox="0 0 1000 392" role="img" aria-label="{label}">
        <defs>
          <marker id="smar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="currentColor"/>
          </marker>
        </defs>
        <g fill="none" stroke="currentColor" stroke-width="1.5">
          <rect x="20"  y="24"  width="215" height="72" rx="2"/>
          <rect x="20"  y="116" width="215" height="88" rx="2"/>
          <rect x="292" y="62"  width="232" height="82" rx="2"/>
          <rect x="20"  y="258" width="215" height="76" rx="2"/>
          <rect x="808" y="60"  width="172" height="130" rx="2"/>
          <rect x="808" y="218" width="172" height="62" rx="2" stroke-dasharray="4 4" opacity=".6"/>
        </g>
        <rect x="292" y="250" width="232" height="90" rx="2" fill="none" stroke="var(--accent)" stroke-width="2"/>
        <rect x="580" y="152" width="172" height="90" rx="2" fill="none" stroke="var(--accent)" stroke-width="2"/>

        <g font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" font-size="12" fill="currentColor">
          <text x="36" y="48" font-weight="600">CloudTrail</text>
          <text x="36" y="66" font-size="11" opacity=".72">CreateApp / DeleteApp</text>
          <text x="36" y="82" font-size="11" opacity=".72">exact spans &#183; {ct_line}</text>

          <text x="36" y="140" font-weight="600">CloudWatch Logs</text>
          <text x="36" y="157" font-size="11" opacity=".72">Insights occupancy</text>
          <text x="36" y="173" font-size="11" opacity=".72">{streams} app log streams</text>
          <text x="36" y="189" font-size="11" opacity=".72">full history, type inferred</text>

          <text x="308" y="86" font-weight="600">Occupancy spans</text>
          <text x="308" y="104" font-size="11" opacity=".72">per space, per day</text>
          <text x="308" y="121" font-size="11" opacity=".72">{observed:,.1f}h observed</text>

          <text x="36" y="282" font-weight="600">Cost Explorer</text>
          <text x="36" y="300" font-size="11" opacity=".72">{billed:,.1f}h billed</text>
          <text x="36" y="316" font-size="11" opacity=".72">${runtime:,.2f} runtime</text>

          <text x="824" y="84" font-weight="600">Split across files</text>
          <text x="824" y="105" font-size="11" opacity=".72">{tier('exec-seconds'):.0f}% exec-seconds</text>
          <text x="824" y="121" font-size="11" opacity=".72">{tier('exec-count'):.0f}% exec-count</text>
          <text x="824" y="137" font-size="11" opacity=".72">{tier('editor-sync'):.0f}% editor-sync</text>
          <text x="824" y="160" font-size="13" font-weight="600">${to_files:,.2f}</text>
          <text x="824" y="176" font-size="10.5" opacity=".62">allocated to files</text>

          <text x="824" y="242" font-weight="600" opacity=".7">No file signal</text>
          <text x="824" y="262" font-size="13" font-weight="600" opacity=".7">${no_signal:,.2f}</text>
        </g>
        <g font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" fill="var(--accent)">
          <text x="308" y="274" font-size="12" font-weight="600">Partition the day</text>
          <text x="308" y="293" font-size="11" fill="currentColor" opacity=".72">each instance type's billed</text>
          <text x="308" y="308" font-size="11" fill="currentColor" opacity=".72">hours divided among the</text>
          <text x="308" y="323" font-size="11" fill="currentColor" opacity=".72">spaces active that day</text>

          <text x="596" y="178" font-size="12" font-weight="600">Space-day cost</text>
          <text x="596" y="203" font-size="17" font-weight="600">{pct(priced)}</text>
          <text x="596" y="221" font-size="11" fill="currentColor" opacity=".75">{allocated:,.1f}h of {billed:,.1f}h</text>
          <text x="596" y="236" font-size="11" fill="currentColor" opacity=".75">priced coverage</text>
        </g>

        <g stroke="currentColor" stroke-width="1.5" fill="none" marker-end="url(#smar)">
          <path d="M235,60 L292,88"/>
          <path d="M235,160 L292,120"/>
          <path d="M408,144 L408,250"/>
          <path d="M235,296 L292,295"/>
          <path d="M524,288 L580,212"/>
          <path d="M752,182 L808,132"/>
          <path d="M752,214 L808,244"/>
        </g>
        <g font-family="ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif" font-size="10.5"
           fill="currentColor" opacity=".62">
          <text x="580" y="300">error bar: {observed:,.1f}h observed against {billed:,.1f}h billed = {pct(obs)}</text>
          <text x="580" y="316">expected slightly above 100%, and never tuned down to it</text>
        </g>
      </svg>
      <figcaption><b>Dollars never leave the bill.</b> Cost Explorer sets what a day cost;
      everything to the right of it decides whose day it was. The two observation sources
      answer different questions &mdash; CloudTrail knows the instance type exactly but only
      reaches back {ct_line}, log occupancy reaches the whole history but has to infer the
      type &mdash; so a row's Source column is the honest record of which one paid for it.
      </figcaption>
    </figure>"""


def _howto(meta: dict) -> str:
    """The method notes, collapsed, with the same summary the Glue dashboard uses.

    Open by default only when the numbers need an explanation before they are
    quoted: a coverage shortfall, an error bar wide enough to mean something is
    double-counted, or unattributed spend worth more than a rounding error.
    Otherwise the reader gets the dashboard first and the caveats on request.
    """
    priced = meta.get("priced_coverage")
    obs = meta.get("observed_coverage")
    unattributed = meta.get("unattributed_cost") or 0
    total = meta.get("total_cost") or 0

    bad_coverage = priced is not None and priced < 0.98
    bad_error_bar = obs is not None and obs > 1.12
    needs_reading = (bad_coverage or bad_error_bar or bool(meta.get("warnings"))
                     or (total and unattributed > total * 0.01))

    warn = ""
    if bad_coverage or bad_error_bar:
        why = []
        if bad_coverage:
            why.append(f"priced coverage is {priced * 100:.1f}%, under the 98% target &mdash; "
                       f"some billed hours could not be placed on any space")
        if bad_error_bar:
            why.append(f"observed runtime is {obs * 100:.1f}% of billed runtime, wide enough "
                       f"to suggest something is double-counted")
        warn = ('<div class="banner warnlevel"><h3>Read this before quoting a number</h3>'
                "<p>" + "; ".join(why) + ".</p></div>")

    return (
        f'<details class="howto" id="howitworks"{" open" if needs_reading else ""}>\n'
        f"  <summary>\n"
        f"    <h2>How this works</h2>\n"
        f'    <span class="tag">method &middot; coverage &middot; caveats</span>\n'
        f'    <span class="chev" aria-hidden="true"></span>\n'
        f"  </summary>\n"
        + warn
        + _method_cards(meta)
        + "\n</details>"
    )


def render(data: dict) -> str:
    meta = data["meta"]

    # Trim the payload: profiles keep only what the page renders.
    slim_profiles = {}
    for path, p in (data.get("profiles") or {}).items():
        slim_profiles[path] = {k: p.get(k) for k in
                               ("libs", "tables", "cells", "code_cells", "kernel",
                                "heavy", "imports", "max_execution_count",
                                "last_modified", "size", "s3_key", "error")
                               if p.get(k) is not None}

    # The page reads three fields out of meta (start, end, storage_cost); everything
    # else it shows was rendered server-side above. Shipping the rest embeds data the
    # dashboard never draws -- notably hash_resolution.unresolved_examples, which is
    # MD5s of the reader's own notebook paths, and occupancy.streams, which is their
    # domain and space ids. This file gets emailed. Send only what it renders.
    PAGE_META = ("account", "region", "start", "end", "total_cost", "storage_cost")
    payload = {
        "meta": {k: meta[k] for k in PAGE_META if k in meta},
        "spaces": data["spaces"],
        "space_days": data["space_days"],
        "recon": data["recon"],
        "notebooks": data["notebooks"],
        "profiles": slim_profiles,
    }
    blob = json.dumps(payload, separators=(",", ":"), default=str)
    blob = blob.replace("</", "<\\/").replace("<!--", "<\\!--")

    # Masthead values. The subtitle counts what the page is about; the account
    # block carries the identity of the run, which is the thing worth checking
    # twice before quoting a number out of it.
    n_spaces = len(data.get("spaces") or {})
    n_files = len({n.get("path") for n in (data.get("notebooks") or []) if n.get("path")})
    subtitle = (f"{n_spaces} Studio space{'' if n_spaces == 1 else 's'} &middot; "
                f"{n_files} notebook{'' if n_files == 1 else 's'} &middot; "
                f"spend attributed to spaces, their owners, each day, and the "
                f"notebooks that actually ran")
    payer_line = (f" &middot; payer <b>{meta['payer_account']}</b>"
                  if meta.get("payer_account") else "")
    split = [f"runtime ${meta['runtime_cost']:,.2f}",
             f"storage ${meta['storage_cost']:,.2f}"]
    if meta.get("other_cost"):
        split.append(f"other ${meta['other_cost']:,.2f}")
    spend_split = " &middot; " + " &middot; ".join(split)
    generated_day = str(meta.get("generated", ""))[:10]
    ce_link = ("https://us-east-1.console.aws.amazon.com/cost-management/home#/cost-explorer"
               f"?chartStyle=STACK&granularity=Daily&groupBy=%5B%22UsageType%22%5D"
               f"&reportType=CostUsage&startDate={meta['start']}&endDate={meta['end']}")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SageMaker Cost Deep Dive {meta['start']} to {meta['end']}</title>
<script>(function(){{var t="light";try{{t=localStorage.getItem("sagemaker-theme")||"light";}}catch(e){{}}document.documentElement.setAttribute("data-theme",t);}})();</script>
<style>{CSS}</style>
</head>
<body>
<div id="tip"></div>
<div class="wrap">

<header class="mast">
  <div>
    <h1>SageMaker Cost Deep Dive</h1>
    <p class="sub">{subtitle}<br>
    billed <b>${meta['total_cost']:,.2f}</b>{spend_split}</p>
  </div>
  <div>
    <div class="acct">
      account <b>{meta['account']}</b> &middot; {meta['region']}<br>
      profile <b>{meta['profile']}</b>{payer_line}<br>
      {meta['start']} &ndash; {meta['end']}<br>
      generated {generated_day}
    </div>
    <button class="tt" type="button" id="themeBtn" title="Switch theme">
      <svg class="ico-moon" viewBox="0 0 20 20" aria-hidden="true">
        <path d="M17 12.3A7.4 7.4 0 0 1 7.7 3 7.4 7.4 0 1 0 17 12.3z" fill="currentColor"/>
      </svg>
      <svg class="ico-sun" viewBox="0 0 20 20" aria-hidden="true">
        <circle cx="10" cy="10" r="3.5" fill="currentColor"/>
        <g stroke="currentColor" stroke-width="1.6" stroke-linecap="round">
          <path d="M10 1.7v2M10 16.3v2M1.7 10h2M16.3 10h2M4.2 4.2l1.4 1.4M14.4 14.4l1.4 1.4M15.8 4.2l-1.4 1.4M5.6 14.4l-1.4 1.4"/>
        </g>
      </svg>
    </button>
  </div>
</header>

<div class="tiles" id="tiles"></div>

<div class="filters">
  <div class="f"><label for="f-from">From</label><input type="date" id="f-from"></div>
  <div class="f"><label for="f-to">To</label><input type="date" id="f-to"></div>
  <div class="f"><label for="f-space">Space</label><select id="f-space"></select></div>
  <div class="f"><label for="f-owner">Owner</label><select id="f-owner"></select></div>
  <div class="f"><label for="f-project">Project</label><select id="f-project"></select></div>
  <div class="f"><label for="f-instance">Instance</label><select id="f-instance"></select></div>
  <div class="f"><label for="f-q">File contains</label><input type="search" id="f-q" placeholder="e.g. .ipynb"></div>
  <button id="reset">Reset</button>
</div>

<h2>Daily cost
  <span class="seg" role="group" aria-label="Break the daily chart down by">
    <button id="mode-space" aria-pressed="true">Space</button>
    <button id="mode-notebook" aria-pressed="false">Notebook</button>
  </span>
  <span class="sub">click a bar to filter to that day &middot; hover for the breakdown</span>
</h2>
<div class="card">
  <p id="chart-note" hidden>
    <b>Allocated, not measured.</b> Space totals are exact; this view divides each space-day
    across the files that ran in it, weighted by cell executions &times; that notebook's own
    seconds-per-cell. A stacked bar reads as fact more readily than a table row does &mdash;
    treat the ranking as reliable and any single bar segment as an estimate. Rows resting on a
    split that carries no real information are flagged <i>wide error</i> in the Files table.
  </p>
  <div class="legend" id="legend-daily"></div>
  <div class="chartbox" id="chart-daily"></div>
</div>

<h2>Spaces <span class="sub">exact &mdash; hours &times; the day's billed share</span></h2>
<div class="scroll" id="tbl-spaces"></div>

<h2>Top files by allocated cost <span class="sub">allocation by logged editor activity, not measured execution</span></h2>
<div class="card"><div class="chartbox" id="chart-files"></div></div>

<h2>Files <span class="sub">what the code imports and reads, from the notebook in S3</span></h2>
<div class="scroll" id="tbl-files"></div>

<details class="sect" id="sect-drill">
  <summary>
    <h2>Day &rarr; space &rarr; file <span class="sub">expand a day to see the split and its basis</span></h2>
    <span class="chev" aria-hidden="true"></span>
  </summary>
  <div id="drill"></div>
</details>

<h2>Reconciliation <span class="sub">attributed hours against billed hours, per day and instance type</span></h2>
<div class="scroll" id="tbl-recon"></div>

<h2>Recommendations</h2>
{_recommendations(data.get("recommendations") or [])}

<h2>Payer-account configuration</h2>
<div class="card">{_payer(data.get("payer") or {})}</div>

{_howto(meta)}

<footer>
  Generated by <code>/aws-cost-audit:sagemaker-cost-analysis</code>.
  Cost Explorer for this period:
  <a href="{ce_link}">open daily usage-type view</a><br>
  Plain-text link: <span class="mono">{ce_link}</span><br>
  Method, reconciliation rules and failure modes:
  <code>plugins/aws-cost-audit/skills/sagemaker-cost-analysis/references/attribution-method.md</code>
</footer>
</div>
<script>
const D = {blob};
{JS}
</script>
</body>
</html>
"""
