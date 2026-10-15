/*
 * Live AI-operator log stream panel (contract §2).
 *
 * - timestamped, severity-coloured rows
 * - auto-scroll (pauses when the operator scrolls up; explicit pause button)
 * - filters: severity chips, node, actor
 * - approve / deny controls inline for `pending_approval` remediations
 * - client-side ring buffer + log_id de-duplication for SSE backfill replays
 */

import { el, clear } from "./dom.js";
import { SEVERITIES, appendLines } from "../core/logs.js";
import { clock, shortId } from "../core/format.js";

export class LogPanel {
  constructor(dom, handlers) {
    this.dom = dom;
    this.handlers = handlers;
    this.lines = [];
    this.seen = new Set();
    this.max = handlers.max || 2000;
    this.autoScroll = true;
    this.filters = { severities: new Set(SEVERITIES), node: "", actor: "" };
    this.nodes = [];
    this.decided = new Set(); // command_ids with an in-flight human decision
    this._pending = []; // rows awaiting the next batched flush (SSE backfill)
    this._flushScheduled = false;
    this._buildSeverityChips();
    this._bind();
    this._renderEmpty();
  }

  _buildSeverityChips() {
    clear(this.dom.severityHost);
    for (const sev of SEVERITIES) {
      const chip = el("button", {
        type: "button",
        class: `chip sev-${sev} on`,
        text: sev,
        dataset: { severity: sev },
        "aria-pressed": "true",
      });
      chip.addEventListener("click", () => {
        const on = this.filters.severities.has(sev);
        if (on) this.filters.severities.delete(sev);
        else this.filters.severities.add(sev);
        chip.classList.toggle("on", !on);
        chip.setAttribute("aria-pressed", String(!on));
        this.render();
      });
      this.dom.severityHost.appendChild(chip);
    }
  }

  _bind() {
    this.dom.pauseBtn.addEventListener("click", () => this.setAutoScroll(!this.autoScroll));
    this.dom.clearBtn.addEventListener("click", () => this.clear());
    this.dom.nodeFilter.addEventListener("change", (e) => {
      this.filters.node = e.target.value;
      this.render();
    });
    this.dom.actorFilter.addEventListener("change", (e) => {
      this.filters.actor = e.target.value;
      this.render();
    });
    this.dom.viewport.addEventListener("scroll", () => {
      if (!this.autoScroll) return;
      const nearBottom = this.dom.viewport.scrollHeight - this.dom.viewport.scrollTop - this.dom.viewport.clientHeight < 24;
      if (!nearBottom) this.setAutoScroll(false, { silent: true });
    });
  }

  setAutoScroll(on, { silent = false } = {}) {
    this.autoScroll = on;
    this.dom.pauseBtn.textContent = on ? "Pause scroll" : "Resume scroll";
    this.dom.pauseBtn.classList.toggle("btn-primary", !on);
    if (on) this._scrollToBottom();
    void silent;
  }

  setNodes(nodes) {
    const prev = this.filters.node;
    this.nodes = nodes || [];
    const ids = this.nodes.map((n) => n.node_id).join("|");
    // rebuild only when the node set actually changed (keeps an open dropdown)
    if (ids === this._nodeIds) return;
    this._nodeIds = ids;
    const sel = this.dom.nodeFilter;
    clear(sel);
    sel.appendChild(el("option", { value: "", text: "all nodes" }));
    for (const n of this.nodes) {
      sel.appendChild(
        el("option", { value: n.node_id, text: n.registration_id || shortId(n.node_id) }),
      );
    }
    if (prev) {
      const still = this.nodes.some((n) => n.node_id === prev);
      sel.value = still ? prev : "";
      this.filters.node = still ? prev : "";
    } else {
      sel.value = "";
    }
  }

  setStreamState(state, detail) {
    const map = { open: "ok", connecting: "warn", reconnecting: "warn", closed: "err", error: "err", stopped: "muted", unsupported: "err", idle: "muted" };
    const cls = map[state] || "muted";
    clear(this.dom.stateHost);
    this.dom.stateHost.appendChild(el("span", { class: `dot dot-${cls}` }));
    this.dom.stateHost.appendChild(document.createTextNode(` ${label(state)}${detail ? ` · ${detail}` : ""}`));
  }

  addLine(line) {
    if (!line) return;
    // §2.3: the control line the server emits when the requested cursor has
    // fallen out of the ring buffer REUSES the oldest retained line's log_id.
    // Registering that id would swallow the first real line of the replay, so
    // control markers never enter the dedupe set.
    if (line.log_id && !line.control) {
      if (this.seen.has(line.log_id)) return;
      this.seen.add(line.log_id);
    }
    this.lines = appendLines(this.lines, [line], this.max);
    if (line.control) {
      // the retained tail follows: say so instead of leaving the panel blank
      this.setStreamState("open", "resumed from the oldest retained line");
    }
    if (this._matches(line)) {
      this.dom.emptyEl.hidden = true;
      this._pending.push(line);
      this._scheduleFlush();
    } else {
      this._updateCount();
    }
  }

  /**
   * Batched DOM flush. A cold-start backfill can deliver thousands of lines at
   * once; appending one row per line would stall the page, so rows are appended
   * in a single fragment on the next frame.
   */
  _scheduleFlush() {
    if (this._flushScheduled) return;
    this._flushScheduled = true;
    const run = () => {
      this._flushScheduled = false;
      const pending = this._pending;
      this._pending = [];
      if (pending.length === 0) {
        this._updateCount();
        return;
      }
      const frag = document.createDocumentFragment();
      for (const line of pending) frag.appendChild(this._row(line));
      this.dom.list.appendChild(frag);
      this._pruneDom();
      if (this.autoScroll) this._scrollToBottom();
      this._updateCount();
    };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
    else setTimeout(run, 16);
  }

  /** Replace the whole buffer (used on Clear + initial backfill). */
  setLines(lines) {
    this.lines = [];
    this.seen.clear();
    for (const l of lines || []) {
      if (l && l.log_id && !l.control) {
        if (this.seen.has(l.log_id)) continue;
        this.seen.add(l.log_id);
      }
      this.lines.push(l);
    }
    if (this.lines.length > this.max) this.lines = this.lines.slice(this.lines.length - this.max);
    this.render();
  }

  clear() {
    this.lines = [];
    this.seen.clear();
    this.render();
  }

  render() {
    clear(this.dom.list);
    this._pending = []; // a full rebuild supersedes anything queued
    const visible = this.lines.filter((l) => this._matches(l));
    if (visible.length === 0) {
      this.dom.emptyEl.hidden = false;
      this.dom.emptyEl.textContent = this.lines.length ? "No lines match the current filters." : "Waiting for operator events…";
    } else {
      this.dom.emptyEl.hidden = true;
      const frag = document.createDocumentFragment();
      for (const l of visible) frag.appendChild(this._row(l));
      this.dom.list.appendChild(frag);
      if (this.autoScroll) this._scrollToBottom();
    }
    this._updateCount();
  }

  _matches(line) {
    if (!this.filters.severities.has(line.severity)) return false;
    if (this.filters.actor && line.actor !== this.filters.actor) return false;
    if (this.filters.node) {
      // keep project-level (null node) lines visible alongside the chosen node
      if (line.node_id && line.node_id !== this.filters.node) return false;
      if (!line.node_id && line.severity === "debug") return false;
    }
    return true;
  }

  _row(line) {
    const rem = line.remediation;
    const cells = [
      el("span", { class: "log-time", text: line.ts ? clock(line.ts) : "—" }),
      el("span", { class: `badge sev-${line.severity}`, text: line.control ? "stream" : line.severity }),
      el("span", { class: "log-node", text: line.node_id ? shortId(line.node_id) : "project" }),
      el("span", { class: "log-msg" }, [
        line.control ? el("span", { class: "truncated", text: "⟳ " }) : null,
        el("span", { text: line.actor === "human" ? `[human] ${line.message}` : line.message }),
      ]),
    ];
    cells.push(this._remediationCell(rem));
    const cls = line.control ? "log-row log-control" : `log-row sev-${line.severity}`;
    return el("li", { class: cls, dataset: { logId: line.log_id || "" } }, cells);
  }

  _remediationCell(rem) {
    if (!rem || !rem.type) return el("span", {});
    const wrap = el("span", { class: "rem" });
    wrap.appendChild(el("span", { class: `rem-status ${rem.status || ""}`, text: `${rem.type}${rem.status ? ` · ${rem.status}` : ""}` }));
    if (rem.status === "pending_approval" && rem.command_id) {
      const busy = this.decided.has(rem.command_id);
      const approve = el("button", { class: "btn btn-ok", type: "button", text: "Approve", disabled: busy ? "disabled" : null });
      const deny = el("button", { class: "btn btn-warn", type: "button", text: "Deny", disabled: busy ? "disabled" : null });
      approve.addEventListener("click", () => {
        approve.disabled = true;
        deny.disabled = true;
        this.handlers.onApprove(rem.command_id, wrap);
      });
      deny.addEventListener("click", () => {
        approve.disabled = true;
        deny.disabled = true;
        this.handlers.onDeny(rem.command_id, wrap);
      });
      wrap.append(approve, deny);
    }
    return wrap;
  }

  _pruneDom() {
    while (this.dom.list.childElementCount > this.max) {
      this.dom.list.removeChild(this.dom.list.firstChild);
    }
  }

  _scrollToBottom() {
    this.dom.viewport.scrollTop = this.dom.viewport.scrollHeight;
  }

  _updateCount() {
    const rows = Array.from(this.dom.list.children);
    const shown = rows.length;
    const buffered = this.lines.length;
    // "0 lines" was the defect this panel was reported with: always state both
    // the rendered rows and what the stream delivered.
    this.dom.countEl.textContent = shown === 0
      ? `${buffered} buffered · none match the filters`
      : `${shown} shown / ${buffered} buffered`;
  }

  _renderEmpty() {
    this.dom.emptyEl.hidden = false;
    this.dom.emptyEl.textContent = "Waiting for operator events…";
  }
}

function label(state) {
  return { open: "live", connecting: "connecting", reconnecting: "reconnecting", closed: "closed", error: "error", stopped: "stopped", unsupported: "unsupported", idle: "idle" }[state] || state;
}
