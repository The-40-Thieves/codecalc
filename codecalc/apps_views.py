"""MCP Apps `ui://` views for `verify_translation` and `verify_optimization`.

See docs/design/2026-09-08-mcp-apps-verification-views.md for the spec facts
this module was built against (extension id, MIME type, `_meta` shape, the
postMessage wire protocol, and why the optimization chart renders medians
only). Two static, self-contained HTML documents — no external `<script src>`
or `<link href>`, no fetch/XHR/WebSocket, inline CSS and JS only — served as
MCP resources under the `ui://` scheme (mime type
`text/html;profile=mcp-app`, imported from the SDK rather than typed as a
literal so it cannot drift from the SDK's own constant).

Each document hand-rolls the small read-only slice of the MCP Apps wire
protocol it needs (a JSON-RPC 2.0 `ui/initialize` handshake over
`window.postMessage`, then a `tool-result` notification carrying
`structuredContent`) rather than loading the reference `app-sdk` package,
because this document cannot fetch anything at all. The listener only acts
on messages whose `event.source` is this document's own `window.parent`
(and only when actually embedded at all), so a sibling frame the host also
happens to embed cannot spoof a tool result.

`debug_html_with_sample_hook()` builds a SEPARATE, screenshot-only copy of a
view wired to `window.__CODECALC_SAMPLE__` — the served documents
(`VERIFY_TRANSLATION_HTML`/`VERIFY_OPTIMIZATION_HTML`, what `server.py`
actually registers) carry no such hook at all, so nothing running in a
real host's iframe can short-circuit the real `tool-result` flow with
fabricated data.

Kept as plain string constants, not a template engine: the server never
substitutes anything into these documents at request time — the tool's
structured result reaches the view entirely client-side, through the host's
own bridge — so there is nothing here for a template engine to parameterise.
"""

from __future__ import annotations

#: Tool name -> the `ui://` resource it is bound to, via `_meta.ui.resourceUri`.
#: The single source both `server.py` (tool `_meta` + resource registration)
#: and `tests/test_mcp_apps_views.py` read from, so the two can never name a
#: different URI for the same tool.
UI_RESOURCE_URIS: dict[str, str] = {
    "verify_translation": "ui://verify-translation/view.html",
    "verify_optimization": "ui://verify-optimization/view.html",
}

#: Self-imposed ceiling (the spec states none) — see the design doc's
#: "Resource shape" section for why this is worth asserting at all.
MAX_VIEW_BYTES = 60_000

#: Shared boilerplate: a small, dependency-free CSS reset that works in both
#: light and dark hosts via the `light-dark()`-adjacent `Canvas`/`CanvasText`
#: system colors (no host theme signal is available to a sandboxed `ui://`
#: document beyond the browser's own color-scheme, so these track that).
_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  margin: 0; padding: 12px 14px; background: Canvas; color: CanvasText;
}
h1 { font-size: 14px; margin: 0 0 10px; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 10px; font-weight: 600; font-size: 12px; }
.badge.pass { background: #1a7f37; color: #fff; }
.badge.fail { background: #cf222e; color: #fff; }
.summary { margin-bottom: 12px; }
table { border-collapse: collapse; width: 100%; font-size: 12px; margin-bottom: 12px; }
th, td { border: 1px solid GrayText; padding: 4px 6px; vertical-align: top; text-align: left; }
th { background: color-mix(in srgb, CanvasText 8%, transparent); }
pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, Menlo, Consolas, monospace; }
.diffline { background: color-mix(in srgb, #cf222e 30%, transparent); }
.outcome-match { color: #1a7f37; font-weight: 600; }
.outcome-mismatch { color: #cf222e; font-weight: 600; }
.outcome-inconclusive { color: #9a6700; font-weight: 600; }
#empty { color: GrayText; }
.legend span { display: inline-block; width: 10px; height: 10px; margin-right: 4px; vertical-align: middle; }
"""

#: The bridge is duplicated (not shared over a network request) because each
#: resource document must be independently self-contained. Handles exactly
#: two things: the `ui/initialize` handshake, and a `tool-result` (or its
#: legacy-unprefixed spelling, tolerated defensively) notification carrying
#: `structuredContent`/`structured_content`. Never calls a tool, never asks
#: for a display-mode change, never sends tool input — a read-only view has
#: no use for any of the protocol's other messages.
#:
#: Origin-checked both ways: `__sendToHost` only posts when this document is
#: actually embedded (`window.parent !== window` — a top-level load has no
#: host to talk to), and the listener drops anything not sourced from that
#: SAME `window.parent` — a sibling frame the host also embeds (an ad, a
#: second tool's own `ui://` view, anything else sharing the same parent)
#: can otherwise post an indistinguishable `tool-result` notification and
#: spoof this view's rendered content. `event.source` is compared to the
#: object, not a string origin, because a `srcdoc`/`about:srcdoc` iframe
#: (how a host typically embeds inline HTML like this) has no meaningful
#: `event.origin` to check.
_BRIDGE_JS = """
var __pending = {};
var __nextId = 1;
var __embedded = window.parent !== window;

function __sendToHost(message) {
  if (__embedded) { window.parent.postMessage(message, "*"); }
}

function __callHost(method, params) {
  return new Promise(function (resolve) {
    var id = __nextId++;
    __pending[id] = resolve;
    __sendToHost({ jsonrpc: "2.0", id: id, method: method, params: params || {} });
  });
}

window.addEventListener("message", function (event) {
  if (!__embedded || event.source !== window.parent) { return; }
  var msg = event.data;
  if (!msg || msg.jsonrpc !== "2.0") { return; }
  if (msg.id !== undefined && __pending[msg.id]) {
    var resolve = __pending[msg.id];
    delete __pending[msg.id];
    resolve(msg.result);
    return;
  }
  if (msg.method === "ui/notifications/tool-result" || msg.method === "notifications/tool-result") {
    var params = msg.params || {};
    render(params.structuredContent || params.structured_content);
  }
});

__callHost("ui/initialize", {}).then(function () {
  __sendToHost({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
});
"""

#: NOT part of `_BRIDGE_JS`, and NEVER included in `VERIFY_TRANSLATION_HTML`/
#: `VERIFY_OPTIMIZATION_HTML` (what `server.py` actually serves). A served
#: `ui://` document that unconditionally honoured a `window.__CODECALC_SAMPLE__`
#: global would let anything able to set a global in that window (a
#: misbehaving extension, a future same-origin script) short-circuit the real
#: `tool-result` flow with fabricated data — so the production documents
#: carry no such check at all. `debug_html_with_sample_hook` below builds a
#: SEPARATE copy, for local screenshot generation only, that appends this
#: hook inside the same closure `render()` is defined in (it is not
#: reachable from outside the IIFE). See "Manual verification" in the design
#: doc.
_SAMPLE_HOOK_JS = """
if (window.__CODECALC_SAMPLE__) { render(window.__CODECALC_SAMPLE__); }
"""

_ESC_JS = """
function esc(s) {
  return String(s === null || s === undefined ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
  });
}
"""


def _document(title: str, body: str, script: str, *, debug_hook: str = "") -> str:
    """`debug_hook` defaults to empty — every production call below leaves it
    unset, so `VERIFY_TRANSLATION_HTML`/`VERIFY_OPTIMIZATION_HTML` (what
    `server.py` serves) never contain it. Only `debug_html_with_sample_hook`
    passes `_SAMPLE_HOOK_JS`, for a screenshot-only copy that is never wired
    into `VIEWS_BY_URI` or any `@mcp.resource`.
    """
    return (
        "<!doctype html>\n"
        "<html>\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{title}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n"
        f"{body}\n"
        f"<script>\n(function () {{\n\"use strict\";\n{_ESC_JS}\n{script}\n{debug_hook}\n{_BRIDGE_JS}\n}})();\n</script>\n"
        "</body>\n</html>\n"
    )


_TRANSLATION_BODY = """
<h1>verify_translation</h1>
<div id="root"><p id="empty">Waiting for tool result&hellip;</p></div>
"""

_TRANSLATION_SCRIPT = """
function firstDiffLine(a, b) {
  var la = String(a || "").split("\\n"), lb = String(b || "").split("\\n");
  var n = Math.max(la.length, lb.length);
  for (var i = 0; i < n; i++) { if (la[i] !== lb[i]) { return i; } }
  return -1;
}

function highlightLines(text, idx) {
  var lines = String(text || "").split("\\n");
  return lines.map(function (line, i) {
    var cls = i === idx ? ' class="diffline"' : "";
    return "<div" + cls + ">" + (esc(line) || "&nbsp;") + "</div>";
  }).join("");
}

function render(result) {
  var root = document.getElementById("root");
  if (!result) { root.innerHTML = '<p id="empty">No result.</p>'; return; }
  var passed = !!result.passed;
  var cases = Array.isArray(result.cases) ? result.cases : [];
  var html = "";
  html += '<div class="summary">';
  html += '<span class="badge ' + (passed ? "pass" : "fail") + '">' + (passed ? "PASSED" : "NOT PASSED") + "</span> ";
  if (result.grade) {
    html += "grade: <b>" + esc(result.grade) + "</b> &mdash; " + esc(result.grade_basis || "") + "<br>";
  }
  html += "matched " + esc(result.matched) + " / mismatched " + esc(result.mismatched) +
          " / inconclusive " + esc(result.inconclusive) + " / total " + esc(result.total);
  if (result.reason) { html += "<br>" + esc(result.reason); }
  html += "</div>";
  html += "<table><thead><tr><th>input</th><th>source stdout</th><th>source raw</th>" +
          "<th>target stdout</th><th>target raw</th><th>outcome</th><th>reason</th></tr></thead><tbody>";
  cases.forEach(function (c) {
    var src = c.source || {}, tgt = c.target || {};
    var idx = c.outcome === "mismatch" ? firstDiffLine(src.stdout_raw, tgt.stdout_raw) : -1;
    html += "<tr>";
    html += "<td><pre>" + esc(c.input) + "</pre></td>";
    html += "<td><pre>" + esc(src.stdout) + "</pre></td>";
    html += "<td><pre>" + highlightLines(src.stdout_raw, idx) + "</pre></td>";
    html += "<td><pre>" + esc(tgt.stdout) + "</pre></td>";
    html += "<td><pre>" + highlightLines(tgt.stdout_raw, idx) + "</pre></td>";
    html += '<td class="outcome-' + esc(c.outcome) + '">' + esc(c.outcome) + "</td>";
    html += "<td>" + esc(c.reason) + "</td>";
    html += "</tr>";
  });
  html += "</tbody></table>";
  root.innerHTML = html;
}
"""

VERIFY_TRANSLATION_HTML = _document(
    "verify_translation", _TRANSLATION_BODY, _TRANSLATION_SCRIPT
)


_OPTIMIZATION_BODY = """
<h1>verify_optimization</h1>
<div id="root"><p id="empty">Waiting for tool result&hellip;</p></div>
"""

#: Bar chart threshold: below this before/after ratio (across every counted
#: size), a linear y-axis stays readable; at or above it a log axis is used
#: so the smallest bars do not collapse to invisible slivers next to the
#: largest. See the design doc for why this renders medians only (the tool's
#: structured result carries no raw per-run samples).
_OPTIMIZATION_SCRIPT = """
var LOG_SCALE_RATIO = 20;

function buildChart(perSize) {
  if (!perSize || !perSize.length) { return "<p>No timing data.</p>"; }
  var w = 640, h = 220, padL = 46, padB = 26, padT = 10, padR = 10;
  var innerW = w - padL - padR, innerH = h - padT - padB;
  var vals = [];
  perSize.forEach(function (r) {
    if (typeof r.before_ms === "number") { vals.push(r.before_ms); }
    if (typeof r.after_ms === "number") { vals.push(r.after_ms); }
  });
  var positive = vals.filter(function (v) { return v > 0; });
  if (!positive.length) { return "<p>No positive timing data.</p>"; }
  var maxV = Math.max.apply(null, positive);
  var minV = Math.min.apply(null, positive);
  var useLog = minV > 0 && (maxV / minV) >= LOG_SCALE_RATIO;

  function scaleY(v) {
    if (typeof v !== "number" || v <= 0) { return innerH; }
    if (useLog) {
      var lv = Math.log(v), lmin = Math.log(minV), lmax = Math.log(maxV);
      var span = lmax - lmin || 1;
      return innerH - ((lv - lmin) / span) * innerH;
    }
    return innerH - (v / maxV) * innerH;
  }

  var n = perSize.length;
  var groupW = innerW / n;
  var barW = Math.min(22, groupW / 3);
  var svg = '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" height="' + h +
            '" role="img" aria-label="before versus after timing per size">';
  svg += '<g transform="translate(' + padL + ',' + padT + ')">';
  perSize.forEach(function (r, i) {
    var cx = i * groupW + groupW / 2;
    var yb = scaleY(r.before_ms), ya = scaleY(r.after_ms);
    svg += '<rect x="' + (cx - barW - 2) + '" y="' + yb + '" width="' + barW +
           '" height="' + Math.max(0, innerH - yb) + '" fill="#888888"></rect>';
    svg += '<rect x="' + (cx + 2) + '" y="' + ya + '" width="' + barW +
           '" height="' + Math.max(0, innerH - ya) + '" fill="#1a7f37"></rect>';
    svg += '<text x="' + cx + '" y="' + (innerH + 16) + '" font-size="10" text-anchor="middle" fill="currentColor">' +
           esc(r.n) + "</text>";
  });
  svg += '<line x1="0" y1="' + innerH + '" x2="' + innerW +
         '" y2="' + innerH + '" stroke="currentColor"></line>';
  svg += "</g></svg>";
  svg += '<div class="legend"><span style="background:#888888"></span>before &nbsp; ' +
         '<span style="background:#1a7f37"></span>after' +
         (useLog ? " &nbsp;(log scale)" : "") + "</div>";
  return svg;
}

function render(result) {
  var root = document.getElementById("root");
  if (!result) { root.innerHTML = '<p id="empty">No result.</p>'; return; }
  var accepted = !!result.accepted;
  var speedup = result.speedup || {};
  var inference = result.inference || {};
  var perSize = Array.isArray(speedup.per_size) ? speedup.per_size : [];
  var infPerSize = Array.isArray(inference.per_size) ? inference.per_size : [];
  var belowFloor = Array.isArray(inference.sizes_below_floor) ? inference.sizes_below_floor : [];
  var html = "";
  html += '<div class="summary">';
  html += '<span class="badge ' + (accepted ? "pass" : "fail") + '">' +
          (accepted ? "ACCEPTED" : "NOT ACCEPTED") + "</span> ";
  if (result.grade) {
    html += "grade: <b>" + esc(result.grade) + "</b> &mdash; " + esc(result.grade_basis || "") + "<br>";
  }
  if (result.reason) { html += esc(result.reason) + "<br>"; }
  if (speedup.ratio !== undefined && speedup.ratio !== null) {
    html += "median ratio: <b>" + esc(speedup.ratio) + "x</b>";
    if (result.min_speedup !== undefined) { html += " (min_speedup " + esc(result.min_speedup) + "x)"; }
    html += "<br>";
  }
  if (inference.decision_basis) { html += esc(inference.decision_basis); }
  html += "</div>";

  html += buildChart(perSize);

  if (infPerSize.length) {
    html += "<table><thead><tr><th>size</th><th>n before</th><th>n after</th>" +
            "<th>U</th><th>p-value</th><th>rank-biserial</th><th>method</th><th>ratio</th></tr></thead><tbody>";
    infPerSize.forEach(function (r) {
      html += "<tr><td>" + esc(r.size) + (r.size_after !== undefined ? (" &rarr; " + esc(r.size_after)) : "") +
              "</td><td>" + esc(r.n_before) + "</td><td>" + esc(r.n_after) +
              "</td><td>" + esc(r.u) + "</td><td>" + esc(r.p_value) +
              "</td><td>" + esc(r.rank_biserial) + "</td><td>" + esc(r.method) +
              "</td><td>" + esc(r.ratio) + "</td></tr>";
    });
    html += "</tbody></table>";
  }

  if (belowFloor.length) {
    html += "<p>sizes excluded from the significance vote:</p>";
    html += "<table><thead><tr><th>size</th><th>before_ms</th><th>after_ms</th><th>reason</th></tr></thead><tbody>";
    belowFloor.forEach(function (r) {
      html += "<tr><td>" + esc(r.size) + "</td><td>" + esc(r.before_ms) + "</td><td>" + esc(r.after_ms) +
              "</td><td>" + esc(r.reason) + "</td></tr>";
    });
    html += "</tbody></table>";
  }

  html += "<div>alpha " + esc(inference.alpha) + " &middot; effective_alpha " +
          esc(inference.effective_alpha) + " &middot; correction " + esc(inference.correction) +
          " &middot; sizes_rejecting " + esc(inference.sizes_rejecting) + "/" + esc(inference.sizes_total) + "</div>";

  root.innerHTML = html;
}
"""

VERIFY_OPTIMIZATION_HTML = _document(
    "verify_optimization", _OPTIMIZATION_BODY, _OPTIMIZATION_SCRIPT
)


#: `resources/read` handler name -> HTML, matching `UI_RESOURCE_URIS` above by
#: construction (asserted in tests/test_mcp_apps_views.py rather than derived
#: automatically, so a mismatch is a loud test failure rather than a KeyError
#: at request time). Every value here is one of the two production calls to
#: `_document()` above, with `debug_hook` unset — this dict is what
#: `server.py` actually serves, so it never carries `_SAMPLE_HOOK_JS`.
VIEWS_BY_URI: dict[str, str] = {
    UI_RESOURCE_URIS["verify_translation"]: VERIFY_TRANSLATION_HTML,
    UI_RESOURCE_URIS["verify_optimization"]: VERIFY_OPTIMIZATION_HTML,
}

#: tool name -> (title, body, script), the same three arguments the two
#: production `_document()` calls above already pass — kept here (rather
#: than re-deriving them from `VIEWS_BY_URI`, which holds the ALREADY-BUILT
#: production HTML with no hook) so `debug_html_with_sample_hook` can build
#: an independent copy with `debug_hook=_SAMPLE_HOOK_JS` instead.
_DOCUMENT_PARTS_BY_TOOL: dict[str, tuple[str, str, str]] = {
    "verify_translation": ("verify_translation", _TRANSLATION_BODY, _TRANSLATION_SCRIPT),
    "verify_optimization": ("verify_optimization", _OPTIMIZATION_BODY, _OPTIMIZATION_SCRIPT),
}


def debug_html_with_sample_hook(tool_name: str) -> str:
    """A screenshot-only variant of a view's HTML, wired to render whatever is
    assigned to `window.__CODECALC_SAMPLE__` before the real handshake would
    otherwise run. NOT served by `server.py`, NOT in `VIEWS_BY_URI` — for
    local, offline rendering (see docs/design/2026-09-08-mcp-apps-
    verification-views.md's "Manual verification") only.
    """
    title, body, script = _DOCUMENT_PARTS_BY_TOOL[tool_name]
    return _document(title, body, script, debug_hook=_SAMPLE_HOOK_JS)
