/* DIAL stats: dependency-free canvas bar chart.
 * Usage: <canvas data-chart="<id of a <script type=application/json>>"> with JSON
 *   {"labels": [...], "series": [{"name": "...", "values": [...], "color": "--css-var"?}, ...]}
 * Colors come from the current theme's CSS custom properties, so dark/light both work; the chart
 * re-renders on resize and when the <html data-theme> attribute changes.
 */
(function () {
  "use strict";

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function palette() {
    return {
      fg: cssVar("--fg", "#e6e8ee"),
      muted: cssVar("--fg-muted", "#a3a9b8"),
      grid: cssVar("--border", "#2a2f3a"),
      series: [cssVar("--primary", "#3b82f6"), cssVar("--ok", "#34d399"), cssVar("--accent", "#22d3ee"),
               cssVar("--warn", "#fbbf24"), cssVar("--err", "#f87171")],
    };
  }

  function seriesColor(s, i, pal) {
    if (s.color && s.color.indexOf("--") === 0) return cssVar(s.color, pal.series[i % pal.series.length]);
    return s.color || pal.series[i % pal.series.length];
  }

  function draw(canvas, data) {
    var pal = palette();
    var labels = data.labels || [];
    var series = (data.series || []).filter(function (s) { return s && s.values; });
    if (!labels.length || !series.length) return;

    var dpr = window.devicePixelRatio || 1;
    var cssW = canvas.clientWidth || canvas.width;
    var cssH = parseInt(canvas.getAttribute("height"), 10) || 260;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.height = cssH + "px";
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var pad = { top: 28, right: 12, bottom: 34, left: 40 };
    var W = cssW - pad.left - pad.right, H = cssH - pad.top - pad.bottom;
    var max = 0;
    series.forEach(function (s) { s.values.forEach(function (v) { if (v > max) max = v; }); });
    if (max === 0) max = 1;
    var steps = 4, unit = Math.max(1, Math.ceil(max / steps));
    var yMax = unit * steps;

    ctx.font = "11px " + cssVar("--font", "system-ui, sans-serif");
    ctx.textBaseline = "middle";
    // gridlines + y labels
    for (var i = 0; i <= steps; i++) {
      var y = pad.top + H - (H * i / steps);
      ctx.strokeStyle = pal.grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left + W, y); ctx.stroke();
      ctx.fillStyle = pal.muted; ctx.textAlign = "right";
      ctx.fillText(String(unit * i), pad.left - 6, y);
    }

    // bars: the first series is the group, later series are overlaid narrower (e.g. answered within calls)
    var n = labels.length, slot = W / n, barW = Math.max(2, slot * 0.7);
    series.forEach(function (s, si) {
      var color = seriesColor(s, si, pal), shrink = si * 0.35;
      var w = Math.max(1, barW * (1 - shrink));
      ctx.fillStyle = color;
      for (var k = 0; k < n; k++) {
        var v = s.values[k] || 0;
        if (!v) continue;
        var h = H * v / yMax;
        var x = pad.left + k * slot + (slot - w) / 2;
        ctx.fillRect(x, pad.top + H - h, w, h);
      }
    });

    // x labels: at most ~12, always the first and the last
    var every = Math.max(1, Math.ceil(n / 12));
    ctx.fillStyle = pal.muted; ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (var j = 0; j < n; j++) {
      if (j % every !== 0 && j !== n - 1) continue;
      ctx.fillText(String(labels[j]), pad.left + j * slot + slot / 2, pad.top + H + 6);
    }

    // legend
    var lx = pad.left; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    series.forEach(function (s, si) {
      ctx.fillStyle = seriesColor(s, si, pal);
      ctx.fillRect(lx, 8, 10, 10);
      ctx.fillStyle = pal.fg;
      var label = s.name || ("#" + (si + 1));
      ctx.fillText(label, lx + 14, 13);
      lx += 14 + ctx.measureText(label).width + 16;
    });
  }

  function render() {
    document.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
      var el = document.getElementById(canvas.getAttribute("data-chart"));
      if (!el) return;
      var data;
      try { data = JSON.parse(el.textContent); } catch (e) { return; }
      draw(canvas, data);
    });
  }

  render();
  var t;
  window.addEventListener("resize", function () { clearTimeout(t); t = setTimeout(render, 150); });
  if (window.MutationObserver) {
    new MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }
})();
