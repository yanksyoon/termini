/*
 * Chat panel — the AI assistant surface (t_d1aa35c8).
 *
 * A docked side panel, reachable from every view via the topbar launcher. It is
 * a management assistant over Termini's own records: each message can carry the
 * element the reader clicked as a removable context chip, and answers cite the
 * rows they rest on as buttons that jump to those rows.
 *
 * Honesty rules this file exists to enforce:
 *   - a PENDING exchange never renders answer text (it is a status line, not a
 *     bubble that can be mistaken for an answer);
 *   - a failed ask renders the controller's typed reason (503 / 422 / 404 /…),
 *     never a generic apology and never a fabricated answer;
 *   - `executed[]` renders what actually ran WITH its command_id + audit_id and
 *     a link to the audit row; an entry without both ids is shown as
 *     unattributed, and an answer that CLAIMS an execution with no executed[]
 *     entry is flagged as a defect on the bubble;
 *   - `?offline=1` answers are labelled fixture data, not model output.
 *
 * Data: POST /v1/projects/{pid}/assistant/ask (controller-api.md §4.10) through
 * the injected `handlers.respond(body)` — the panel owns no URL and no credential.
 */

import { el, clear } from "./dom.js";
import {
  AssistantSession,
  contextChip,
  suggestionsFor,
  auditCitationFor,
} from "../core/assistant.js";
import { clock, num, shortId } from "../core/format.js";

export class ChatPanel {
  constructor(dom, handlers = {}) {
    this.dom = dom;
    this.handlers = handlers;
    this.session = new AssistantSession();
    this.offline = !!handlers.offline;
    this.open = false;
    this.busy = false;
    this._bind();
    this.renderMode();
    this.renderContext();
    this.renderSuggestions();
    this.render();
  }

  /* ------------------------------------------------------------- lifecycle */

  /** `?offline=1` — the answers are fixtures; the panel says so, permanently. */
  setOffline(v) {
    this.offline = !!v;
    this.renderMode();
  }

  _bind() {
    const d = this.dom;
    if (d.launcher) d.launcher.addEventListener("click", () => this.toggle());
    if (d.closeBtn) d.closeBtn.addEventListener("click", () => this.hide());
    if (d.contextClear) d.contextClear.addEventListener("click", () => this.clearContext());
    if (d.form) {
      d.form.addEventListener("submit", (e) => {
        if (e && e.preventDefault) e.preventDefault();
        const text = d.input ? d.input.value : "";
        if (d.input) d.input.value = "";
        this.ask(text);
      });
    }
    if (d.input) {
      d.input.addEventListener("keydown", (e) => {
        if (e && e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          const text = d.input.value;
          d.input.value = "";
          this.ask(text);
        }
      });
    }
    if (typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("keydown", (e) => {
        if (e && e.key === "Escape" && this.open) this.hide();
      });
    }
  }

  show() {
    this.open = true;
    if (this.dom.panel) this.dom.panel.hidden = false;
    if (typeof document !== "undefined" && document.body && document.body.classList) {
      document.body.classList.add("chat-open");
    }
    if (this.dom.launcher) this.dom.launcher.setAttribute("aria-expanded", "true");
    this.scrollToEnd();
    if (this.handlers.onOpen) this.handlers.onOpen();
  }

  hide() {
    this.open = false;
    if (this.dom.panel) this.dom.panel.hidden = true;
    if (typeof document !== "undefined" && document.body && document.body.classList) {
      document.body.classList.remove("chat-open");
    }
    if (this.dom.launcher) {
      this.dom.launcher.setAttribute("aria-expanded", "false");
      if (typeof this.dom.launcher.focus === "function") this.dom.launcher.focus();
    }
    if (this.handlers.onClose) this.handlers.onClose();
  }

  toggle() {
    if (this.open) this.hide();
    else this.show();
  }

  focusInput() {
    if (this.dom.input && typeof this.dom.input.focus === "function") this.dom.input.focus();
  }

  /* ---------------------------------------------------------------- context */

  /**
   * Attach the element the reader clicked and bring the panel forward. The chip
   * survives every follow-up question until it is removed or another element is
   * attached (`AssistantSession.attachContext`).
   */
  attachContext(input) {
    const ctx = this.session.attachContext(input);
    this.show();
    this.renderContext();
    this.renderSuggestions();
    this.focusInput();
    return ctx;
  }

  clearContext() {
    this.session.clearContext();
    this.renderContext();
    this.renderSuggestions();
    this.focusInput();
  }

  renderContext() {
    const d = this.dom;
    const chip = contextChip(this.session.context);
    const host = d.contextHost;
    if (host) host.hidden = !this.session.context;
    if (d.contextKind) d.contextKind.textContent = chip.kind === "none" ? "" : chip.kind;
    if (d.contextLabel) {
      const text = this.session.context ? chip.text.replace(`${chip.kind} · `, "") : "";
      d.contextLabel.textContent = text;
      d.contextLabel.setAttribute("title", chip.title);
    }
    if (d.contextChip) {
      d.contextChip.dataset.contextKind = this.session.context ? chip.kind : "";
      d.contextChip.dataset.contextId = this.session.context ? chip.boundId : "";
      d.contextChip.dataset.contextDecisionId = chip.decisionId || "";
      d.contextChip.dataset.contextText = this.session.context ? chip.text : "";
    }
    if (d.contextClear) d.contextClear.disabled = !this.session.context;
  }

  renderSuggestions() {
    const host = this.dom.suggestions;
    if (!host) return;
    clear(host);
    const list = suggestionsFor(this.session.context ? this.session.context.kind : "none");
    for (const s of list) {
      const b = el("button", {
        type: "button",
        class: "chip chat-suggestion",
        text: s,
        title: "ask this, with the attached context",
      });
      b.addEventListener("click", () => this.ask(s));
      host.appendChild(b);
    }
  }

  renderMode() {
    const host = this.dom.modeEl;
    if (!host) return;
    host.className = `badge ${this.offline ? "chat-mode-fixture" : "chat-mode-grounded"}`;
    host.textContent = this.offline ? "fixture data · offline demo" : "grounded in controller records";
    host.setAttribute(
      "title",
      this.offline
        ? "offline demo: answers come from bundled fixtures, not from a model"
        : "answers are grounded in the controller's own records for the attached context",
    );
  }

  /* ------------------------------------------------------------------ ask */

  async ask(question) {
    const text = String(question || "").trim();
    if (!text || this.busy) return;
    if (!this.open) this.show();
    this.busy = true;
    this.setSendEnabled(false);
    const begun = this.session.begin(text);
    this.render();
    this.scrollToEnd();
    try {
      const response = await this.handlers.respond(begun.body);
      this.session.settle(begun.messageId, response || {}, { fixture: this.offline });
    } catch (err) {
      this.session.fail(begun.messageId, err);
    } finally {
      this.busy = false;
      this.setSendEnabled(true);
      this.render();
      this.scrollToEnd();
    }
  }

  retry(messageId) {
    const msg = this.session.find(messageId);
    if (!msg || !msg.question) return;
    // Drop the failed bubble and re-ask with the same question. The message id
    // is not reused: a retry is a new message (and a new idempotency anchor),
    // while the conversation id stays the same.
    const idx = this.session.messages.indexOf(msg);
    if (idx > 0) this.session.messages.splice(idx, 1);
    this.render();
    this.ask(msg.question);
  }

  setSendEnabled(on) {
    if (this.dom.sendBtn) this.dom.sendBtn.disabled = !on;
  }

  /* --------------------------------------------------------------- render */

  render() {
    const host = this.dom.messages;
    if (!host) return;
    clear(host);
    // Once a conversation exists the suggested follow-ups give way to the
    // answers: the panel is a chat, not a wall of prompts.
    if (this.dom.suggestions) this.dom.suggestions.hidden = this.session.messages.length > 0;
    if (this.session.messages.length === 0) {
      host.appendChild(
        el("li", { class: "chat-empty muted small" }, [
          el("div", { text: "Ask about an incident, a node, or anything Termini did to your nodes." }),
          el("div", {
            class: "muted small",
            text: "Use “Ask AI” on a card, decision row, chart, pending remediation or audit row to attach it to the conversation.",
          }),
        ]),
      );
      return;
    }
    for (const m of this.session.messages) {
      host.appendChild(m.role === "user" ? this._userBubble(m) : this._assistantBubble(m));
    }
  }

  _userBubble(m) {
    return el("li", { class: "chat-msg chat-msg-user", dataset: { messageId: m.id } }, [
      el("div", { class: "chat-who", text: "you" }),
      el("p", { class: "chat-text", text: m.text }),
    ]);
  }

  _assistantBubble(m) {
    const li = el("li", {
      class: `chat-msg chat-msg-assistant chat-state-${m.state}`,
      dataset: { messageId: m.id, state: m.state },
    });
    li.appendChild(el("div", { class: "chat-who", text: "assistant" }));

    if (m.state === "pending") {
      // NOT an answer: a status line, marked busy, with no answer text at all.
      li.setAttribute("aria-busy", "true");
      li.appendChild(
        el("div", { class: "chat-pending", role: "status" }, [
          el("span", { class: "chat-spinner", "aria-hidden": "true" }, [el("span", { class: "dot dot-warn" })]),
          el("span", { text: "asking the controller… no answer yet" }),
        ]),
      );
      return li;
    }

    if (m.state === "error") {
      const e = m.error || { badge: "ERROR", message: "the request failed", kind: "error" };
      li.setAttribute("role", "alert");
      li.appendChild(
        el("div", { class: `chat-error chat-error-${e.kind}`, dataset: { errorCode: e.code, status: String(e.status) } }, [
          el("div", { class: "chat-error-head" }, [
            el("span", { class: "badge chat-err-badge", text: e.badge }),
            el("span", { class: "chat-error-kind", text: errorKindLabel(e.kind) }),
            e.requestId
              ? el("span", { class: "muted small", text: `request ${shortId(e.requestId, 10, 4)}`, title: e.requestId })
              : null,
          ]),
          el("p", { class: "chat-error-text", text: e.message }),
          el("div", { class: "chat-error-actions" }, [
            el("button", {
              type: "button",
              class: "btn btn-sm",
              text: "Retry",
              onclick: () => this.retry(m.id),
            }),
          ]),
        ]),
      );
      return li;
    }

    // answered
    const meta = [
      m.decidedBy ? `decided_by ${m.decidedBy}` : null,
      m.model ? `model ${m.model}` : null,
      typeof m.elapsedMs === "number" ? `${num(m.elapsedMs, 0)} ms` : null,
      m.at ? `answered ${clock(m.at)}` : null,
    ].filter(Boolean);

    if (m.fixture) {
      li.appendChild(
        el("div", { class: "chat-fixture", title: "offline demo: bundled fixture, not a model answer" }, [
          el("span", { class: "badge chat-fixture-badge", text: "fixture data" }),
          el("span", { text: "offline demo answer — not a model answer, no controller behind it" }),
        ]),
      );
    }

    li.appendChild(el("p", { class: "chat-text chat-answer", text: m.text }));

    if (m.claimDefect) {
      li.appendChild(
        el("div", { class: "chat-defect", role: "alert", dataset: { defect: "unbacked_execution_claim" } }, [
          el("span", { class: "badge chat-defect-badge", text: "unverified claim" }),
          el(
            "span",
            {
              text:
                "this answer claims an execution but carries no executed[] entry — there is no command_id or audit_id to check. Treat it as unverified (contract §4.10).",
            },
          ),
        ]),
      );
    }

    if (m.citations.length) {
      li.appendChild(this._citations(m.citations));
    }

    if (m.executed.length) {
      li.appendChild(this._executed(m.executed));
    }

    if (m.proposed.length) {
      li.appendChild(
        el("div", { class: "chat-proposed" }, [
          el("div", {
            class: "chat-label",
            text: "proposed by the model — not executed",
          }),
          el(
            "ul",
            { class: "chat-proposed-list" },
            m.proposed.map((p) =>
              el("li", {}, [
                el("code", { text: p.action }),
                p.target ? el("span", { class: "chat-target", text: p.target, title: p.target }) : null,
              ]),
            ),
          ),
        ]),
      );
    }

    if (m.truncated) {
      li.appendChild(
        el("div", { class: "chat-note chat-truncated" }, [
          el("span", { class: "badge", text: "context truncated" }),
          el("span", { text: "the controller trimmed the context to fit the prompt budget — ask about one record at a time for a complete answer." }),
        ]),
      );
    }

    if (m.errors.length) {
      li.appendChild(
        el("ul", { class: "chat-note chat-errors" }, m.errors.map((err) => {
          const reason = (err && (err.reason || err.code || err.message)) || "error";
          const detail =
            err && (err.message || err.kind || err.id)
              ? ` · ${err.message || (err.kind ? `${err.kind} ${err.id || ""}`.trim() : "")}`
              : "";
          return el("li", { dataset: { errorReason: String(reason) }, text: `${reason}${detail}` });
        })),
      );
    }

    if (meta.length) {
      li.appendChild(el("div", { class: "chat-answer-meta muted small", text: meta.join(" · ") }));
    }
    return li;
  }

  _citations(list) {
    const row = el("div", { class: "chat-citations" }, [
      el("span", { class: "chat-label", text: "citations" }),
    ]);
    for (const c of list) {
      row.appendChild(
        el("button", {
          type: "button",
          class: "chip chat-cite",
          text: `${c.kind} · ${c.label}`,
          title: `${c.kind} ${c.id} — jump to it`,
          dataset: { citeKind: c.kind, citeId: c.id },
          onclick: () => {
            if (this.handlers.onCitation) this.handlers.onCitation(c);
          },
        }),
      );
    }
    return row;
  }

  /**
   * What actually ran. Every row carries its command_id and audit_id and a
   * button that opens the audit row itself; a row missing either id is shown as
   * unattributed rather than as a success.
   */
  _executed(list) {
    const box = el("div", { class: "chat-executed" }, [
      el("div", { class: "chat-label", text: "executed — what actually ran" }),
    ]);
    for (const ex of list) {
      const rows = [
        el("div", { class: "chat-exec-head" }, [
          el("span", { class: `badge chat-exec-state chat-exec-${ex.state}`, text: ex.state }),
          el("code", { class: "chat-exec-action", text: ex.action }),
          ex.target ? el("span", { class: "chat-exec-target", text: ex.target, title: ex.target }) : null,
        ]),
      ];
      if (ex.attributed) {
        rows.push(
          el("div", { class: "chat-exec-ids" }, [
            el("span", { class: "muted small", text: "command " }),
            el("code", { class: "chat-exec-id", text: ex.commandId, title: ex.commandId }),
            el("span", { class: "muted small", text: "audit " }),
            el("code", { class: "chat-exec-id", text: ex.auditId, title: ex.auditId }),
            el("button", {
              type: "button",
              class: "linklike chat-exec-link",
              text: "open audit row",
              title: `audit ${ex.auditId}`,
              onclick: () => {
                const cite = auditCitationFor(ex);
                if (cite && this.handlers.onCitation) this.handlers.onCitation(cite);
              },
            }),
          ]),
        );
      } else {
        rows.push(
          el("div", { class: "chat-defect", role: "alert" }, [
            el("span", { class: "badge chat-defect-badge", text: "unattributed" }),
            el("span", {
              text: "this execution carries no command_id/audit_id — it cannot be verified against the audit surface (§4.10, D1)",
            }),
          ]),
        );
      }
      if (ex.reason) rows.push(el("div", { class: "chat-exec-reason", text: ex.reason }));
      box.appendChild(el("div", { class: "chat-exec-row", dataset: { auditId: ex.auditId, commandId: ex.commandId } }, rows));
    }
    return box;
  }

  scrollToEnd() {
    const v = this.dom.messages;
    if (v && typeof v.scrollTop === "number" && typeof v.scrollHeight === "number") {
      v.scrollTop = v.scrollHeight;
    }
  }
}

function errorKindLabel(kind) {
  switch (kind) {
    case "model_unavailable":
      return "the model did not answer — no answer was invented";
    case "invalid_argument":
      return "the controller refused the request";
    case "not_found":
      return "the controller has no record for that id";
    case "unauthenticated":
      return "the controller refused this credential";
    case "network":
      return "the controller could not be reached";
    default:
      return "the request failed";
  }
}
