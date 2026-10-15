/*
 * Dependency-free canvas time-series chart. Renders one metric group with:
 *   - labelled X axis (local HH:MM) and Y axis with the group's unit
 *   - horizontal/vertical gridlines
 *   - one coloured polyline per series, with a legend
 *   - a hover crosshair with per-series values
 *   - an explicit "no data" state so an empty window never looks broken
 *
 * No third-party code, no build step — matches the controller's stdlib-only
 * philosophy and keeps the demo load-bearing.
 */

import { hhmm } from "./core/format.js";

const PALETTE = ["#5ac8fa", "#ff9f0a", "#30d158", "#ff375f", "#bf5af2", "#64d2ff", "#ffd60a"];

/* Decision markers are coloured by WHO decided, so a fallback never reads as a
 * model decision on the chart either. */
const MARKER_COLORS = { model: "#5ac8fa", fallback: "#ff9f0a", deterministic: "#bf5af2", det: "#bf5af2" };
const THRESHOLD_COLOR = "#ff375f";

export class TimeSeriesChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.group = null;
    this.hover = null;
    this.pad = { l: 56, r: 12, t: 12, b: 24 };
    this._onMove = (e) => this._pointer(e, true);
    this._onLeave = () => {
      this.hover = null;
      this.draw();
    };
    canvas.addEventListener("mousemove", this._onMove);
    canvas.addEventListener("mouseleave", this._onLeave);
    canvas.addEventListener("touchmove", this._onMove, { passive: true });
    canvas.addEventListener("touchend", this._onLeave);
  }

  setData(group) {
    this.group = group;
    this.draw();
  }

  destroy() {
    this.canvas.removeEventListener("mousemove", this._onMove);
    this.canvas.removeEventListener("mouseleave", this._onLeave);
    this.canvas.removeEventListener("touchmove", this._onMove);
    this.canvas.removeEventListener("touchend", this._onLeave);
  }

  get colorOf() {
    return (i) => PALETTE[i % PALETTE.length];
  }

  _bounds() {
    const g = this.group;
    let tMin = Infinity;
    let tMax = -Infinity;
    let vMax = 0;
    let count = 0;
    for (const s of g.series) {
      for (const p of s.points) {
        if (p.t < tMin) tMin = p.t;
        if (p.t > tMax) tMax = p.t;
        if (p.v > vMax) vMax = p.v;
        count += 1;
      }
    }
    if (!Number.isFinite(tMin) || !Number.isFinite(tMax) || count === 0) return null;
    if (tMin === tMax) tMax = tMin + 1000;
    for (const th of g.thresholds || []) {
      if (Number.isFinite(th.value) && th.value > vMax) vMax = th.value;
    }
    let top = g.yMax !== undefined ? g.yMax : niceMax(vMax);
    if (top <= 0) top = 1;
    return { tMin, tMax, top, count };
  }

  draw() {
    const canvas = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || canvas.parentElement?.clientWidth || 320;
    const cssH = canvas.clientHeight || 140;
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const W = cssW;
    const H = cssH;
    const { l, r, t, b } = this.pad;
    const plotW = Math.max(1, W - l - r);
    const plotH = Math.max(1, H - t - b);

    ctx.fillStyle = "rgba(255,255,255,0.02)";
    ctx.fillRect(l, t, plotW, plotH);

    const g = this.group;
    const bounds = g ? this._bounds() : null;

    if (!g || !bounds) {
      ctx.fillStyle = "#8b95a5";
      ctx.font = "13px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("no data for this window", W / 2, H / 2);
      return;
    }

    const { tMin, tMax, top } = bounds;
    const xOf = (tt) => l + ((tt - tMin) / (tMax - tMin)) * plotW;
    const yOf = (v) => t + plotH - (Math.max(0, v) / top) * plotH;

    // gridlines + Y ticks
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.fillStyle = "#8b95a5";
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const v = (top * i) / 4;
      const y = yOf(v);
      ctx.beginPath();
      ctx.moveTo(l, y);
      ctx.lineTo(l + plotW, y);
      ctx.stroke();
      ctx.fillText(axisValue(v, top), l - 6, y);
    }

    // X ticks
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (let i = 0; i <= 4; i++) {
      const tt = tMin + ((tMax - tMin) * i) / 4;
      const x = xOf(tt);
      ctx.beginPath();
      ctx.moveTo(x, t);
      ctx.lineTo(x, t + plotH);
      ctx.stroke();
      ctx.fillText(hhmm(tt), x, t + plotH + 5);
    }

    // series lines
    g.series.forEach((s, si) => {
      ctx.strokeStyle = this.colorOf(si);
      ctx.lineWidth = 2;
      ctx.beginPath();
      let started = false;
      for (const p of s.points) {
        const x = xOf(p.t);
        const y = yOf(p.v);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
      if (s.points.length === 1) {
        const p = s.points[0];
        ctx.fillStyle = this.colorOf(si);
        ctx.beginPath();
        ctx.arc(xOf(p.t), yOf(p.v), 3, 0, Math.PI * 2);
        ctx.fill();
      }
    });

    // hover crosshair + tooltip
    if (this.hover) {
      const hx = Math.min(Math.max(this.hover.x, l), l + plotW);
      const tt = tMin + ((hx - l) / plotW) * (tMax - tMin);
      ctx.strokeStyle = "rgba(255,255,255,0.35)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(hx, t);
      ctx.lineTo(hx, t + plotH);
      ctx.stroke();
      ctx.setLineDash([]);

      const rows = g.series.map((s, si) => ({ color: this.colorOf(si), label: s.label, value: nearest(s.points, tt) }));
      drawTooltip(ctx, hx, t, l + plotW, rows, g.unit);
    }
    // thresholds: the line the telemetry crossed (drawn under the series)
    for (const th of g.thresholds || []) {
      if (!Number.isFinite(th.value)) continue;
      const y = yOf(th.value);
      if (y < t - 1 || y > t + plotH + 1) continue;
      ctx.save();
      ctx.strokeStyle = THRESHOLD_COLOR;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(l, y);
      ctx.lineTo(l + plotW, y);
      ctx.stroke();
      ctx.restore();
      ctx.fillStyle = THRESHOLD_COLOR;
      ctx.font = "10px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText(clip(`${th.label}`, 54), l + 4, y - 3);
    }

    // decision markers: WHEN the operator acted, coloured by who decided
    let lastLabelX = -999;
    for (const mk of g.markers || []) {
      if (!Number.isFinite(mk.t) || mk.t < tMin || mk.t > tMax) continue;
      const x = xOf(mk.t);
      const color = MARKER_COLORS[mk.kind] || "#8b95a5";
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, t);
      ctx.lineTo(x, t + plotH);
      ctx.stroke();
      ctx.restore();
      // flag at the top of the line
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(x, t);
      ctx.lineTo(x + 7, t + 4.5);
      ctx.lineTo(x, t + 9);
      ctx.closePath();
      ctx.fill();
      // one label per cluster, so overlapping decisions stay readable
      if (x - lastLabelX > 48) {
        ctx.fillStyle = color;
        ctx.font = "10px system-ui, sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.fillText(clip(mk.label, 22), Math.min(x + 9, l + plotW - 60), t + 1);
        lastLabelX = x;
      }
    }
  }

  _pointer(e, active) {
    const rect = this.canvas.getBoundingClientRect();
    const x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
    this.hover = active ? { x } : null;
    this.draw();
  }
}

function nearest(points, tt) {
  if (!points.length) return null;
  let best = points[0];
  let bestD = Math.abs(best.t - tt);
  for (const p of points) {
    const d = Math.abs(p.t - tt);
    if (d < bestD) {
      best = p;
      bestD = d;
    }
  }
  return best.v;
}

/** Truncate a chart label so a long rationale never bleeds across the plot. */
function clip(text, max) {
  const s = String(text ?? "");
  return s.length > max ? `${s.slice(0, Math.max(0, max - 1))}…` : s;
}

function niceMax(v) {
  if (v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = 10 ** exp;
  const n = v / base;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return step * base;
}

function axisValue(v, top) {
  const digits = top <= 5 ? 2 : top <= 20 ? 1 : 0;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return v.toFixed(digits);
}

function drawTooltip(ctx, x, top, rightEdge, rows, unit) {
  const lines = rows.filter((r) => r.value !== null);
  if (!lines.length) return;
  ctx.font = "12px system-ui, sans-serif";
  const padX = 8;
  const lineH = 16;
  const w = Math.min(220, Math.max(120, rightEdge - 20));
  const h = padX * 2 + lines.length * lineH;
  let bx = x + 10;
  if (bx + w > rightEdge) bx = x - 10 - w;
  const by = top + 6;
  ctx.fillStyle = "rgba(12,16,24,0.92)";
  ctx.strokeStyle = "rgba(255,255,255,0.18)";
  ctx.lineWidth = 1;
  roundedRect(ctx, bx, by, w, h, 6);
  ctx.fill();
  ctx.stroke();
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  lines.forEach((r, i) => {
    const y = by + padX + i * lineH + lineH / 2;
    ctx.fillStyle = r.color;
    ctx.fillRect(bx + padX, y - 4, 8, 8);
    ctx.fillStyle = "#e6e9ef";
    const val = `${trim(r.value)} ${unit === "percent" ? "%" : unitLabel(unit)}`.trim();
    ctx.fillText(`${r.label}  ${val}`, bx + padX + 14, y);
  });
}

function unitLabel(unit) {
  if (unit === "bytes") return "GiB";
  if (unit === "percent") return "%";
  return unit || "";
}

function trim(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (Math.abs(n) >= 100) return n.toFixed(0);
  if (Math.abs(n) >= 10) return n.toFixed(1);
  return n.toFixed(2);
}

function roundedRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
