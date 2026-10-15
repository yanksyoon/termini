/*
 * "Your node" panel — the client-facing registration + health view
 * (task t_5649d117; frozen contract on t_074b1a14, option (a)).
 *
 * Renders, for ONE node and with that node's OWN credential:
 *   - registration state (pending / registered / degraded / not reporting),
 *   - when it enrolled and when it was last seen,
 *   - live first telemetry: heartbeat, CPU, memory, runtime facts.
 *
 * The credential is read from the two inputs in the panel and handed to the
 * caller (`onConnect`), which keeps it in this tab's sessionStorage only. It is
 * never put in a URL, and this module never builds a link containing it.
 *
 * All state derivation lives in `../core/client-node.js` (pure + unit-tested);
 * this module is only markup and event wiring.
 */

import { emptyState, healthCards, oneLiner, STATE } from "../core/client-node.js";

const TONE_DOT = { ok: "dot-ok", warn: "dot-warn", err: "dot-err", muted: "dot-muted" };

export class MyNodePanel {
  /**
   * @param els {empty, live, stateText, stateDot, detail, cards, error, nodeId,
   *            token, connectBtn, forgetBtn, command, credentialsHelp,
   *            enrolledAt, lastSeen}
   * @param handlers {onConnect({nodeId, nodeToken}), onForget()}
   */
  constructor(els, handlers = {}) {
    this.dom = els;
    this.handlers = handlers;
    this.controller = "";
    this.credential = { nodeId: "", nodeToken: "" };

    if (this.dom.connectBtn) {
      this.dom.connectBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        const nodeId = (this.dom.nodeId && this.dom.nodeId.value || "").trim();
        const nodeToken = (this.dom.token && this.dom.token.value || "").trim();
        if (this.handlers.onConnect) this.handlers.onConnect({ nodeId, nodeToken });
      });
    }
    if (this.dom.forgetBtn) {
      this.dom.forgetBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        if (this.handlers.onForget) this.handlers.onForget();
      });
    }
  }

  /** The controller base, used for the one-liner in the empty state. */
  setController(url) {
    this.controller = url || "";
    this.renderEmptyState();
  }

  setCredential({ nodeId = "", nodeToken = "" } = {}) {
    this.credential = { nodeId, nodeToken };
    if (this.dom.nodeId) this.dom.nodeId.value = nodeId;
    if (this.dom.token) this.dom.token.value = nodeToken;
    this.renderEmptyState();
  }

  hasCredential() {
    return !!(this.credential.nodeId && this.credential.nodeToken);
  }

  /** Render the instructive empty state (REQUIRED WORK 5). */
  renderEmptyState() {
    const es = emptyState({ controller: this.controller });
    if (this.dom.empty) {
      this.dom.empty.replaceChildren();
      const h = document.createElement("p");
      h.className = "mynode-headline";
      h.textContent = es.headline;
      this.dom.empty.appendChild(h);
      const lead = document.createElement("p");
      lead.className = "muted";
      lead.textContent = es.lead;
      this.dom.empty.appendChild(lead);
      const ol = document.createElement("ol");
      ol.className = "mynode-steps";
      for (const step of es.steps) {
        const li = document.createElement("li");
        li.appendChild(document.createTextNode(step.text));
        if (step.code) {
          const pre = document.createElement("code");
          pre.className = "mynode-cmd";
          pre.textContent = step.code;
          li.appendChild(pre);
        }
        ol.appendChild(li);
      }
      this.dom.empty.appendChild(ol);
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "Once your node is enrolled you will see: " + es.promise.join("; ") + ".";
      this.dom.empty.appendChild(p);
    }
    if (this.dom.command) this.dom.command.textContent = oneLiner(this.controller);
  }

  /**
   * Render one poll result.
   * @param view result of `registrationView()` (state/label/tone/detail)
   * @param summary result of `healthSummary()` (or null)
   * @param opts {endpoint, enrolledHint}
   */
  render(view, summary, opts = {}) {
    const hasCred = this.hasCredential();
    if (this.dom.empty) this.dom.empty.hidden = hasCred;
    if (this.dom.live) this.dom.live.hidden = !hasCred;
    if (!hasCred) {
      this.renderEmptyState();
      return;
    }
    if (this.dom.stateDot) {
      this.dom.stateDot.className = `dot ${TONE_DOT[view.tone] || "dot-muted"}`;
    }
    if (this.dom.stateText) this.dom.stateText.textContent = view.label;
    if (this.dom.detail) this.dom.detail.textContent = view.detail || "";
    if (this.dom.enrolledAt) this.dom.enrolledAt.textContent = opts.stateValue || "—";
    if (this.dom.lastSeen) {
      this.dom.lastSeen.textContent = view.ageS === null ? "no sample yet" : `${Math.round(view.ageS)}s ago`;
    }
    if (this.dom.endpoint) this.dom.endpoint.textContent = opts.endpoint || "";

    if (this.dom.error) {
      const bad = view.state === STATE.CREDENTIAL_REJECTED || view.state === STATE.CONTROLLER_UNREACHABLE ||
        view.state === STATE.UNREACHABLE;
      this.dom.error.hidden = !bad;
      this.dom.error.textContent = bad ? view.detail : "";
    }

    if (this.dom.cards) {
      this.dom.cards.replaceChildren();
      const cards = healthCards(summary);
      if (!cards.length) {
        const p = document.createElement("p");
        p.className = "muted small";
        p.textContent = view.detail || "";
        this.dom.cards.appendChild(p);
      }
      for (const c of cards) {
        const card = document.createElement("div");
        card.className = "mynode-card";
        const label = document.createElement("span");
        label.className = "mynode-card-label";
        label.textContent = c.label;
        const value = document.createElement("span");
        value.className = "mynode-card-value";
        value.textContent = c.value;
        card.appendChild(label);
        card.appendChild(value);
        if (c.hint) {
          const hint = document.createElement("span");
          hint.className = "mynode-card-hint muted";
          hint.textContent = c.hint;
          card.appendChild(hint);
        }
        this.dom.cards.appendChild(card);
      }
    }
  }
}
