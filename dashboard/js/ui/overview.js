/*
 * Overview grid — per-node status/health cards (contract §1.1/§1.3).
 * Driven by `GET /nodes` + a latest telemetry sample per node.
 */

import { el, clear } from "./dom.js";
import { nodeView, REASON_LABELS } from "../core/health.js";
import { pct, bytes, ago, uptime, shortId } from "../core/format.js";

export function renderOverview(container, state, handlers) {
  clear(container);
  const nodes = state.nodes || [];

  if (state.loading && nodes.length === 0) {
    for (let i = 0; i < 3; i++) container.appendChild(el("div", { class: "skeleton" }));
    return;
  }
  if (nodes.length === 0) {
    container.appendChild(
      el("div", { class: "empty" }, [
        el("div", { text: "No nodes registered yet." }),
        el("div", { class: "muted small", text: "Nodes appear here as soon as they register with the controller." }),
      ]),
    );
    return;
  }

  for (const node of nodes) {
    const sample = (state.samples || new Map()).get(node.node_id) || null;
    const view = nodeView(node, sample, state.now);
    container.appendChild(card(node, sample, view, state, handlers));
  }
}

function card(node, sample, view, state, handlers) {
  const status = view.status || "offline";
  const health = view.health || {};
  const selected = state.selectedNodeId === node.node_id;

  const head = el("div", { class: "node-head" }, [
    el("div", { class: "node-ident" }, [
      el("div", {
        class: "node-name",
        text: node.registration_id || shortId(node.node_id),
        // The name is truncated in the card (a registration_id is 32 chars), so
        // carry the full value on the element for hover/discovery.
        title: node.registration_id || node.node_id,
      }),
      el("div", { class: "node-sub", text: shortId(node.node_id), title: node.node_id }),
    ]),
    el("div", { class: "flag-row" }, [
      el("span", { class: `badge role-${node.role || "compute"}`, text: node.role || "compute" }),
      // contract §1.1: a `synthetic: true` row is fabricated by the demo seeder,
      // so it is labelled rather than presented as hardware.
      node.synthetic ? el("span", { class: "badge synthetic", text: "demo", title: "fabricated by the demo seeder (synthetic: true)" }) : null,
      el("span", { class: `badge status-${status}`, text: status }),
    ]),
  ]);

  const metrics = el("div", { class: "metrics-row" }, [
    metric("CPU", health.cpu_util_pct === null || health.cpu_util_pct === undefined ? "—" : pct(health.cpu_util_pct)),
    metric("RAM", health.ram_used_pct === null || health.ram_used_pct === undefined ? "—" : pct(health.ram_used_pct)),
    metric("Disk", diskState(health, sample)),
  ]);

  const flags = el("div", { class: "flag-row" }, [
    flag("disk", health.disk_ok),
    flag("gpu", health.gpu_ok),
    flag("inference", health.inference_ok),
  ]);

  const svc = el("div", { class: "services" });
  for (const s of health.services || []) {
    svc.appendChild(el("span", { class: `flag ${s.status === "running" ? "ok" : "bad"}`, text: `${s.name}:${s.status}` }));
  }

  // Top processes (contract §4.10) — what is running *inside* the node. Metrics
  // only: name, CPU, RSS. The panel shows the first few of the bounded top-20;
  // the rest are on the node's charts/detail view.
  const procs = el("div", { class: "processes" });
  const top = (health.processes || []).slice(0, 4);
  if (top.length) {
    procs.appendChild(el("span", { class: "processes-label", text: "top processes" }));
    for (const p of top) {
      const cpu = typeof p.cpu_util_pct === "number" ? pct(p.cpu_util_pct) : "—";
      const label = `${p.name || `pid ${p.pid}`} · ${cpu} · ${bytes(p.ram_used_bytes)}`;
      procs.appendChild(el("span", { class: "process", text: label, title: `pid ${p.pid}${p.state ? ` · ${p.state}` : ""}` }));
    }
  }

  const reasons = el("div", { class: "reasons" });
  for (const r of view.reasons || []) {
    reasons.appendChild(el("span", { class: "reason", text: REASON_LABELS[r] || r }));
  }

  const foot = el("div", { class: "node-foot" }, [
    el("span", { text: `seen ${view.staleMs === null ? "never" : ago(node.last_seen_at, state.now)}` }),
    el("span", { text: `up ${uptime((sample && sample.uptime_sec) ?? node.uptime_sec)}` }),
  ]);

  const actions = el("div", { class: "btn-row" }, [
    el("button", {
      class: "btn btn-sm",
      type: "button",
      text: selected ? "● Charts" : "Charts",
      title: "Show this node's metrics and controls",
      onclick: () => handlers.onSelect(node.node_id),
    }),
    el("button", {
      class: "btn btn-sm btn-warn",
      type: "button",
      text: "Restart",
      onclick: () => handlers.onReboot(node.node_id),
    }),
    el("button", {
      class: "btn btn-sm btn-warn",
      type: "button",
      text: "Drain",
      onclick: () => handlers.onDrain(node.node_id),
    }),
  ]);

  return el("div", { class: `node-card status-${status}`, dataset: { nodeId: node.node_id } }, [
    head,
    metrics,
    flags,
    reasons,
    svc,
    procs,
    foot,
    actions,
  ]);
}

function metric(k, v) {
  return el("div", { class: "metric" }, [el("div", { class: "k", text: k }), el("div", { class: "v", text: v })]);
}

function flag(name, value) {
  const cls = value === true ? "ok" : value === false ? "bad" : "null";
  const label = value === true ? "ok" : value === false ? "FAIL" : "n/a";
  return el("span", { class: `flag ${cls}`, text: `${name} ${label}` });
}

function diskState(health, sample) {
  const label = health && health.disk_ok === false ? "read-only" : health && health.disk_ok === true ? "ok" : "—";
  const free = diskFree(sample);
  return free === "—" ? label : `${label} · ${free}`;
}

function diskFree(sample) {
  if (!sample || !Array.isArray(sample.disk) || sample.disk.length === 0) return "—";
  const total = sample.disk.reduce((a, d) => a + (Number(d.total_bytes) || 0), 0);
  const free = sample.disk.reduce((a, d) => a + (Number(d.free_bytes) || 0), 0);
  if (!total) return "—";
  return `${bytes(free, { digits: 1 })} free`;
}
