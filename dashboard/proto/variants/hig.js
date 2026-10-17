/*
 * VARIANT 4 — "Overview" (task t_6af4e389)
 *
 * Additive design module, mounted only while this variant is active. The
 * harness owns the picker and toggles the design layer; this file is the one
 * axis CSS cannot express: a different INFORMATION ARCHITECTURE.
 *
 * The prototype page carries the shipped markup id-for-id and boots the shipped
 * modules unmodified (see harness.js). Those modules bind to ids once, at load,
 * and own their data. So this module never re-implements behaviour: it MOVES
 * the shipped nodes into three destinations (Fleet / Incidents / Settings),
 * collapses each repeatable record into a row plus a detail pane, and puts a
 * single overflow menu on a row. Every listener, poll, stream and honesty
 * surface keeps running on the same objects.
 *
 * Everything it moves is recorded (a snapshot of each original parent's child
 * list) and everything it creates is owned, so leaving the variant restores the
 * shipped page exactly — the three other variants are judged on the original
 * markup, not on this module's leftovers.
 *
 * Three deliberate choices worth naming:
 *   - The selected node is READ, not owned: `#chart-node` is the shipped
 *     selection (main.js keeps it in sync), so a selection made anywhere else
 *     (a chart citation jump, the node <select>) moves the detail pane too.
 *   - Incident detail is the shipped row's own children, relocated. A poll that
 *     rebuilds a row simply gives the pane a fresh subtree on the next tick.
 *   - Approvals are the one thing allowed to interrupt: they have a 300 s clock
 *     (contract §4.4), so they surface as a badge plus one banner that holds the
 *     shipped Approve / Deny controls. The countdown is labelled with where its
 *     clock starts, because only the controller knows the true issue time.
 */

import { el } from "../../js/ui/dom.js";

const VARIANT = "hig";
const DESTINATIONS = ["fleet", "incidents", "settings"];
const LABELS = { fleet: "Fleet", incidents: "Incidents", settings: "Settings" };
const APPEARANCE_KEY = "termini.hig.appearance";
const APPROVAL_WINDOW_MS = 300_000; // contract §4.4: pending_approval auto-denies at 300 s
const RECONCILE_MS = 250;

const ICONS = {
  fleet:
    '<svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">' +
    '<rect x="2.6" y="2.6" width="6.2" height="6.2" rx="1.7"/><rect x="11.2" y="2.6" width="6.2" height="6.2" rx="1.7"/>' +
    '<rect x="2.6" y="11.2" width="6.2" height="6.2" rx="1.7"/><rect x="11.2" y="11.2" width="6.2" height="6.2" rx="1.7"/></svg>',
  incidents:
    '<svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">' +
    '<circle cx="10" cy="10" r="7.3"/><path d="M10 5.9v4.7" stroke-linecap="round"/>' +
    '<circle cx="10" cy="13.8" r="0.85" fill="currentColor" stroke="none"/></svg>',
  settings:
    '<svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">' +
    '<path d="M2.8 6.2h14.4M2.8 13.8h14.4" stroke-linecap="round"/>' +
    '<circle cx="7.2" cy="6.2" r="1.9" fill="var(--hig-bg)"/><circle cx="13" cy="13.8" r="1.9" fill="var(--hig-bg)"/></svg>',
};

/* -------------------------------------------------------------- bookkeeping */

let mounted = false;
let shell = null;
let sweep = null;
let clock = null;
const ui = {};
const state = { dest: "fleet", appearance: "system", decisionId: null };

/** Original child lists of shipped parents, captured before the first move. */
const snapshots = new Map();
/** Nodes this module created inside shipped containers, removed on unmount. */
let owned = [];
/** Approximate approval clocks: command id -> when this tab first saw it. */
const approvals = new Map();

function own(node) {
  owned.push(node);
  return node;
}

function noteParent(parent) {
  if (!parent || parent.nodeType !== 1 || snapshots.has(parent)) return;
  snapshots.set(parent, [...parent.childNodes]);
}

/** Move a shipped node, remembering enough to put it back exactly. */
function adopt(node, parent, before = null) {
  if (!node) return null;
  noteParent(node.parentNode);
  noteParent(parent);
  parent.insertBefore(node, before);
  return node;
}

/** Put every shipped child back where it was, then drop what we created. */
function restore() {
  for (const [parent, kids] of snapshots) {
    kids.forEach((kid, i) => {
      if (parent.childNodes[i] !== kid) parent.insertBefore(kid, parent.childNodes[i] || null);
    });
  }
  snapshots.clear();
  for (const node of owned) node.remove();
  owned = [];
}

/** A plain div owned by this module. */
function box(cls, attrs = {}, children = []) {
  return own(el("div", { class: cls, ...attrs }, children));
}

/* ------------------------------------------------------------------- shell */

function buildShell() {
  const layout = document.querySelector("main.layout");
  const bannerHost = document.getElementById("banner-host");
  const overviewPanel = document.querySelector(".panel-overview");
  const decisionsPanel = document.querySelector(".panel-decisions");
  const metricsPanel = document.querySelector(".panel-metrics");
  const controlsPanel = document.querySelector(".panel-controls");
  const mynodePanel = document.querySelector(".panel-mynode");
  const logPanel = document.getElementById("log-panel");
  const settingsBtn = document.getElementById("btn-settings");
  const terminalLink = document.getElementById("link-terminal");
  const chatBtn = document.getElementById("btn-chat");
  const topbar = document.querySelector("header.topbar");

  /* ---------------------------------------------------------- destinations */
  const navItems = [];
  const nav = el("nav", { class: "hig-nav", "aria-label": "Destinations" });
  for (const dest of DESTINATIONS) {
    const btn = el(
      "button",
      { class: "hig-nav-item", type: "button", dataset: { dest } },
      [el("span", { class: "hig-nav-icon", html: ICONS[dest] }), el("span", { class: "hig-nav-label", text: LABELS[dest] })],
    );
    if (dest === "incidents") {
      ui.navBadge = el("span", { class: "hig-badge", "aria-hidden": "true", hidden: "" });
      btn.appendChild(ui.navBadge);
    }
    btn.addEventListener("click", () => go(dest, { focusView: true }));
    navItems.push(btn);
    nav.appendChild(btn);
  }
  ui.navItems = navItems;

  ui.title = el("h1", { class: "hig-title", id: "hig-title", text: "Fleet" });
  ui.subtitle = el("p", { class: "hig-sub", id: "hig-sub" });
  ui.back = el("button", { class: "hig-back", type: "button", text: "Back" });
  ui.back.addEventListener("click", () => {
    document.documentElement.removeAttribute("data-hig-detail");
    ui.back.blur();
  });
  ui.logBtn = el("button", { class: "hig-btn", type: "button", text: "Operator log" });
  ui.logBtn.addEventListener("click", openSheet);
  ui.sheetBtn = el("button", { class: "hig-btn", type: "button", text: "Open the operator log" });
  ui.sheetBtn.addEventListener("click", openSheet);

  const bar = el("header", { class: "hig-bar" }, [
    el("div", { class: "hig-bar-title" }, [ui.title, ui.subtitle]),
    el("div", { class: "hig-bar-actions" }, [ui.back, ui.logBtn, chatBtn]),
  ]);

  /* ---------------------------------------------------------------- views */
  ui.nodeRest = box("hig-node-rest", { id: "hig-node-rest" });
  ui.nodeStore = box("hig-store", { hidden: "" });
  ui.nodeTitle = el("h2", { class: "hig-detail-title", id: "hig-node-title", text: "Node" });
  ui.nodeStatus = el("p", { class: "hig-detail-sub", id: "hig-node-status" });
  ui.fleetState = el("div", { class: "hig-state", id: "hig-fleet-state", role: "status" });

  const fleetPane = el("div", { class: "hig-list-pane", "aria-label": "Nodes" });
  fleetPane.appendChild(ui.fleetState);
  adopt(overviewPanel, fleetPane);

  const fleetDetail = el(
    "div",
    { class: "hig-detail-pane", id: "hig-node-detail", role: "region", "aria-labelledby": "hig-node-title" },
    [ui.nodeTitle, ui.nodeStatus, ui.nodeRest, ui.nodeStore, metricsPanel, controlsPanel],
  );

  const fleetSplit = el("div", { class: "hig-split" }, [fleetPane, fleetDetail]);

  ui.approvals = el("div", { class: "hig-approvals", id: "hig-approvals", role: "region", "aria-label": "Approvals", hidden: "" });
  ui.approvalCount = el("strong", { class: "hig-approvals-count" });
  ui.approvalClock = el("span", { class: "hig-approvals-clock" });
  ui.approvalItems = el("div", { class: "hig-approvals-items", id: "hig-approval-items" });
  ui.approvals.append(
    el("div", { class: "hig-approvals-text" }, [ui.approvalCount, ui.approvalClock]),
    ui.approvalItems,
  );

  ui.decisionDetail = el("div", {
    class: "hig-detail-pane",
    id: "hig-incident-detail",
    role: "region",
    "aria-label": "Selected decision",
    tabindex: "-1",
  });
  ui.decisionEmpty = el("p", { class: "hig-detail-empty", id: "hig-incident-empty", text: "No decision selected." });
  ui.decisionDetail.appendChild(ui.decisionEmpty);
  ui.decisionStore = box("hig-store", { hidden: "" });

  const incidentPane = el("div", { class: "hig-list-pane", "aria-label": "Decisions" });
  adopt(decisionsPanel, incidentPane);
  // "How this list is built" — the decider strip, the join-key note and the
  // counts are provenance, not the list. They stay in the destination, one
  // disclosure deeper, so a reader who wants to check the join can, and a
  // reader scanning what needs them is not made to scroll past it every time.
  const about = el("details", { class: "hig-about" }, [
    el("summary", { class: "hig-about-summary", text: "How this list is built — decider, join keys and counts" }),
  ]);
  const deciderEl = decisionsPanel && decisionsPanel.querySelector("#decider");
  const noteEl = decisionsPanel && decisionsPanel.querySelector(".decision-note");
  const countEl = decisionsPanel && decisionsPanel.querySelector("#decisions-count");
  for (const node of [deciderEl, noteEl, countEl]) if (node) adopt(node, about);
  incidentPane.appendChild(about);

  const incidentSplit = el("div", { class: "hig-split" }, [incidentPane, ui.decisionDetail]);
  const incidentView = el("section", { class: "hig-view", dataset: { view: "incidents" }, "aria-labelledby": "hig-title" }, [
    ui.approvals,
    incidentSplit,
    ui.decisionStore,
  ]);

  /* -------------------------------------------------------------- settings */
  ui.appearanceBtns = ["system", "light", "dark"].map((mode) =>
    el("button", {
      class: "hig-seg-item",
      type: "button",
      text: mode === "system" ? "System" : mode === "light" ? "Light" : "Dark",
      dataset: { appearance: mode },
    }),
  );
  for (const b of ui.appearanceBtns) {
    b.addEventListener("click", () => setAppearance(b.dataset.appearance, { persist: true }));
  }

  const settings = el("div", { class: "hig-settings" }, [
    el("section", { class: "hig-section", "aria-labelledby": "hig-h-appearance" }, [
      el("h2", { class: "hig-h2", id: "hig-h-appearance", text: "Appearance" }),
      el("p", { class: "hig-note", text: "Follows the system setting unless you choose otherwise. This tab only." }),
      el("div", { class: "hig-seg", role: "group", "aria-label": "Appearance" }, ui.appearanceBtns),
    ]),
    el("section", { class: "hig-section", "aria-labelledby": "hig-h-connection" }, [
      el("h2", { class: "hig-h2", id: "hig-h-connection", text: "Connection" }),
      el("p", { class: "hig-note", text: "The controller URL, the project id and the credential live in this tab only." }),
      el("div", { class: "hig-row-actions" }, [settingsBtn, terminalLink].filter(Boolean)),
    ]),
    el("section", { class: "hig-section", "aria-labelledby": "hig-h-enroll" }, [
      el("h2", { class: "hig-h2", id: "hig-h-enroll", text: "Enroll a node" }),
      el("p", {
        class: "hig-note",
        text: "Connect this browser to one node with that node's own credential. Nothing is enrolled until you press Show my node.",
      }),
      ...(mynodePanel ? [mynodePanel] : []),
    ]),
    el("section", { class: "hig-section", "aria-labelledby": "hig-h-diagnostics" }, [
      el("h2", { class: "hig-h2", id: "hig-h-diagnostics", text: "Diagnostics" }),
      el("p", { class: "hig-note", text: "The unparsed operator stream and the raw evidence behind a decision open here, over the page." }),
      el("div", { class: "hig-row-actions" }, [ui.sheetBtn]),
    ]),
  ]);

  ui.views = {
    fleet: el("section", { class: "hig-view", dataset: { view: "fleet" }, "aria-labelledby": "hig-title" }, [fleetSplit]),
    incidents: incidentView,
    settings: el("section", { class: "hig-view", dataset: { view: "settings" }, "aria-labelledby": "hig-title" }, [settings]),
  };

  /* ------------------------------------------------------------ log sheet */
  const sheetDone = el("button", { class: "hig-btn", type: "button", text: "Done" });
  ui.sheet = el("dialog", { class: "hig-sheet", id: "hig-sheet", "aria-label": "Operator log" }, []);
  const sheetHead = el("header", { class: "hig-sheet-head" }, [
    el("h2", { class: "hig-h2", text: "Operator log" }),
    el("span", { class: "hig-note", text: "raw stream — the unparsed evidence behind the incident list" }),
    sheetDone,
  ]);
  ui.sheet.appendChild(sheetHead);
  if (logPanel) adopt(logPanel, ui.sheet);
  sheetDone.addEventListener("click", () => ui.sheet.close());
  ui.sheet.addEventListener("close", () => ui.logBtn.focus());
  document.body.appendChild(ui.sheet);

  /* ---------------------------------------------------------- connection */
  const conn = el("div", { class: "hig-conn" });
  for (const id of ["pill-nodes", "pill-backend", "pill-stream"]) {
    const pill = document.getElementById(id);
    if (pill) conn.appendChild(pill);
  }
  const time = document.getElementById("clock");
  if (time) conn.appendChild(time);

  const sidebar = el("aside", { class: "hig-sidebar", "aria-label": "Termini" }, [
    el("div", { class: "hig-brand" }, [
      el("span", { class: "hig-brand-mark", "aria-hidden": "true", text: "◇" }),
      el("span", { class: "hig-brand-name", text: "Termini" }),
    ]),
    nav,
    conn,
  ]);

  const content = el("div", { class: "hig-content" }, [bannerHost || el("div"), bar, el("main", { class: "hig-main" }, Object.values(ui.views))]);
  shell = el("div", { class: "hig-shell", id: "hig-shell" }, [sidebar, content]);
  document.body.appendChild(shell);

  // The shipped topbar is the only element with no home in the new IA: its
  // brand is rebuilt above, and its controls moved into the bar/sidebar. Keep
  // the node itself (not delete it) so a restore is exact.
  if (topbar) ownInvisible(topbar);
  // `main.layout` is now an empty shell: its grid/padding would leave a gap at
  // the end of the page. Marked (not removed) so a restore is exact.
  if (layout) {
    layout.setAttribute("data-hig-layout", "");
    owned.push({ remove: () => layout.removeAttribute("data-hig-layout") });
  }
}

/**
 * "Own" a node that must stay in the document but out of the layout: it is
 * restored by the snapshot machinery and hidden by CSS, never removed early.
 */
function ownInvisible(node) {
  node.setAttribute("data-hig-hidden", "");
  owned.push({ remove: () => node.removeAttribute("data-hig-hidden") });
}

/* ---------------------------------------------------------------- routing */

function go(dest, { focusView = false } = {}) {
  if (!DESTINATIONS.includes(dest)) dest = "fleet";
  state.dest = dest;
  document.documentElement.dataset.higDest = dest;
  document.documentElement.removeAttribute("data-hig-detail");

  for (const btn of ui.navItems) {
    const on = btn.dataset.dest === dest;
    btn.classList.toggle("hig-on", on);
    btn.setAttribute("aria-current", on ? "page" : "false");
  }
  for (const view of Object.values(ui.views)) view.hidden = view.dataset.view !== dest;
  ui.title.textContent = LABELS[dest];

  if (focusView) {
    // Land the keyboard in the destination, not back at the top of the page.
    const first = ui.views[dest].querySelector(
      "button:not([tabindex='-1']), [href], input, select, textarea, [tabindex='0']",
    );
    (first || ui.views[dest]).focus?.({ preventScroll: true });
  }
  syncSubtitle();
  const url = new URL(location.href);
  url.searchParams.set("dest", dest);
  history.replaceState(null, "", url);
}

function syncSubtitle() {
  if (state.dest === "fleet") {
    const cards = [...document.querySelectorAll("#overview .node-card")];
    const degraded = cards.filter((c) => c.classList.contains("status-degraded")).length;
    const offline = cards.filter((c) => c.classList.contains("status-offline")).length;
    const bits = [`${cards.length} node${cards.length === 1 ? "" : "s"}`];
    if (degraded) bits.push(`${degraded} ${degraded === 1 ? "needs" : "need"} attention`);
    if (offline) bits.push(`${offline} offline`);
    const updated = document.getElementById("overview-updated");
    if (updated && updated.textContent && updated.textContent !== "—") bits.push(updated.textContent);
    ui.subtitle.textContent = bits.join(" · ");
  } else if (state.dest === "incidents") {
    const rows = [...document.querySelectorAll("#decision-list > .decision-row")];
    const fallback = rows.filter((r) => r.classList.contains("src-fallback")).length;
    const bits = [`${rows.length} decision${rows.length === 1 ? "" : "s"}`];
    if (fallback) bits.push(`${fallback} by fallback`);
    ui.subtitle.textContent = bits.join(" · ");
  } else {
    ui.subtitle.textContent = "Connection, appearance and enrolling a node";
  }
}

/* --------------------------------------------------------------- fleet rows
 * A node becomes ONE row: name, semantic status dot, CPU / RAM / disk, and the
 * reason when degraded. Everything else (flags, services, processes, uptime) is
 * moved into the detail pane; the row's only control is an overflow menu.
 */

function decorateCards() {
  const host = document.getElementById("overview");
  if (!host) return;
  const cards = [...host.querySelectorAll(".node-card")];
  for (const card of cards) {
    if (card.dataset.higRow) continue;
    noteParent(card);
    decorateCard(card);
  }
  syncFleetState(cards);
}

function decorateCard(card) {
  card.dataset.higRow = "1";
  card.classList.add("hig-row");

  const kids = [...card.children];
  const pick = (cls) => kids.find((k) => k.classList.contains(cls)) || null;
  const head = pick("node-head");
  const metrics = pick("metrics-row");
  const reasons = pick("reasons");
  const extras = kids.filter((k) =>
    ["flag-row", "services", "processes", "node-foot"].some((cls) => k.classList.contains(cls)),
  );

  const name = head && head.querySelector(".node-name");
  const label = (name && (name.getAttribute("title") || name.textContent)) || card.dataset.nodeId || "node";

  // The row's primary target: a real focusable control the size of the row, so
  // Tab reaches it and Enter opens the node. No nested interactive elements:
  // it holds the shipped head + figures, the menu button is its sibling.
  const open = own(el("div", { class: "hig-row-open", role: "button", tabindex: "0", "aria-label": `Open ${label}` }));
  if (head) adopt(head, open);
  if (metrics) adopt(metrics, open);
  if (reasons) adopt(reasons, open);
  open.addEventListener("click", (ev) => {
    if (ev.target.closest("button, a")) return;
    openNode(card);
  });
  open.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      openNode(card);
    } else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      moveNodeFocus(card, ev.key === "ArrowDown" ? 1 : -1);
    }
  });
  card.insertBefore(open, card.firstChild);

  // Detail: the parts that are not the row's job.
  const rest = own(el("div", { class: "hig-node-rest" }));
  for (const node of extras) adopt(node, rest);
  card.appendChild(rest);

  // One overflow menu per row, holding the shipped action buttons. Destructive
  // items stay in the menu and keep their own confirmation dialog.
  const btnRow = pick("btn-row");
  const menu = own(el("div", { class: "hig-menu", role: "menu", "aria-label": `Actions for ${label}`, hidden: "" }));
  if (btnRow) {
    adopt(btnRow, menu);
    for (const btn of btnRow.querySelectorAll("button")) {
      btn.setAttribute("role", "menuitem");
      if (btn.classList.contains("btn-warn")) btn.classList.add("hig-destructive");
    }
  }
  const menuBtn = own(
    el("button", {
      class: "hig-menu-btn",
      type: "button",
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      "aria-label": `Actions for ${label}`,
      text: "•••",
    }),
  );
  menuBtn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    toggleMenu(card, menu, menuBtn);
  });
  card.append(menuBtn, menu);
}

function openNode(card) {
  const chartsBtn = card.querySelector(".btn-row [title^='Show this node']");
  if (chartsBtn) chartsBtn.click();
  selectNodeRow(card);
  if (window.matchMedia("(max-width: 820px)").matches) {
    document.documentElement.dataset.higDetail = "fleet";
    ui.back.focus({ preventScroll: true });
  }
}

function selectNodeRow(card) {
  for (const other of document.querySelectorAll("#overview .node-card")) {
    other.setAttribute("aria-current", other === card ? "true" : "false");
  }
}

function moveNodeFocus(card, delta) {
  const cards = [...document.querySelectorAll("#overview .node-card")];
  const i = cards.indexOf(card);
  const next = cards[i + delta];
  if (next) next.querySelector(".hig-row-open")?.focus();
}

/** Park each node's extra parts in the detail pane, or in the store. */
function placeNodeDetail() {
  const cards = [...document.querySelectorAll("#overview .node-card")];
  const select = document.getElementById("chart-node");
  const selectedId = (select && select.value) || null;
  const selected = cards.find((c) => c.dataset.nodeId === selectedId) || cards[0] || null;

  // The node panel rebuilds its cards on every poll, so a parked `.hig-node-rest`
  // outlives the card it came from. Without this, each poll appends a fresh copy
  // and the detail pane grows a second, stale block. Drop parked copies whose
  // node is gone, and replace a rebuilt card's copy with the new one.
  const live = new Set(cards.map((c) => c.dataset.nodeId));
  for (const host of [ui.nodeRest, ui.nodeStore]) {
    for (const rest of [...host.children].filter((n) => n.classList.contains("hig-node-rest"))) {
      if (!live.has(rest.dataset.higCard)) rest.remove();
    }
  }

  for (const card of cards) {
    const rest = card.querySelector(".hig-node-rest");
    if (!rest) continue;
    rest.dataset.higCard = card.dataset.nodeId;
    if (card === selected) {
      ui.nodeRest.querySelectorAll(".hig-node-rest").forEach((n) => n.remove());
      adopt(rest, ui.nodeRest);
    } else {
      ui.nodeStore
        .querySelectorAll(`.hig-node-rest[data-hig-card="${card.dataset.nodeId}"]`)
        .forEach((n) => n.remove());
      adopt(rest, ui.nodeStore);
    }
  }

  if (selected) {
    const name = selected.querySelector(".node-name");
    const status = (selected.className.match(/status-(\w+)/) || [])[1] || "unknown";
    ui.nodeTitle.textContent = (name && (name.getAttribute("title") || name.textContent)) || "Node";
    ui.nodeStatus.textContent = `Status: ${status}. Charts and actions below apply to this node.`;
    selectNodeRow(selected);
  }
}

/* ----------------------------------------------------------- incidents rows
 * Master–detail: the row keeps its one-line head; the record itself (incident
 * card, thinking log, breach, rationale, audit links) moves into the pane.
 */

function listRows() {
  return [...document.querySelectorAll("#decision-list > .decision-row")];
}

function decorateDecisions() {
  const list = document.getElementById("decision-list");
  if (!list) return;
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", "Operator decisions");

  const rows = listRows();
  for (const row of rows) {
    if (!row.dataset.higOption) {
      row.dataset.higOption = "1";
      row.classList.add("hig-row");
      row.setAttribute("role", "option");
    }
  }
  if (!rows.some((r) => r.dataset.decisionId === state.decisionId)) {
    state.decisionId = rows[0] ? rows[0].dataset.decisionId : null;
  }
  for (const row of rows) {
    const on = row.dataset.decisionId === state.decisionId;
    row.tabIndex = on ? 0 : -1;
    row.setAttribute("aria-selected", on ? "true" : "false");
  }
}

function selectDecision(id) {
  state.decisionId = id;
  const rows = listRows();
  const row = rows.find((r) => r.dataset.decisionId === id);
  if (!row) return;
  for (const other of rows) {
    const on = other === row;
    other.tabIndex = on ? 0 : -1;
    other.setAttribute("aria-selected", on ? "true" : "false");
  }
  row.focus({ preventScroll: true });
  row.scrollIntoView({ block: "nearest" });
  placeDecisionDetail();
  if (window.matchMedia("(max-width: 820px)").matches) {
    document.documentElement.dataset.higDetail = "incidents";
    ui.decisionDetail.focus({ preventScroll: true });
  }
}

function placeDecisionDetail() {
  const rows = listRows();
  const ids = new Set(rows.map((r) => r.dataset.decisionId));
  const selected = rows.find((r) => r.dataset.decisionId === state.decisionId) || null;
  const keep = selected ? selected.dataset.decisionId : null;

  // Every record body lives either in the pane (the selected record) or in the
  // parked store (all the others). A row is a list row — its head and nothing
  // else — so the option never nests a button or a link, and the page is one
  // screen whatever the history's length. The old version only parked parts
  // that had already reached the pane, so every unselected row kept its whole
  // incident card inline: 75 KB of evidence in the list, and 45 interactive
  // elements nested inside an `option`.
  for (const row of rows) {
    const mine = row === selected;
    for (const part of [...row.children]) {
      if (part.classList.contains("decision-head")) continue;
      part.dataset.higPart = row.dataset.decisionId;
      adopt(part, mine ? ui.decisionDetail : ui.decisionStore);
    }
  }
  // Anything still in the pane that belongs to another record (a poll rebuilt
  // the row, or the fetch window moved on) is parked too.
  for (const node of [...ui.decisionDetail.children]) {
    if (node.dataset && node.dataset.higPart && node.dataset.higPart !== keep) {
      ui.decisionStore.appendChild(node);
    }
  }
  ui.decisionEmpty.hidden = !!selected;

  // Parked parts of decisions the fetch window no longer holds are dropped.
  for (const node of [...ui.decisionStore.children]) {
    if (!ids.has(node.dataset.higPart)) node.remove();
  }
}

/* --------------------------------------------------------------- approvals
 * One banner, only when something is genuinely time-boxed: the shipped Approve
 * / Deny controls move into it. The badge on the destination is the standing,
 * quiet signal — no permanent queue on the landing page.
 */

function syncApprovals() {
  const host = document.getElementById("pending-list");
  if (!host) return;
  const items = [...host.querySelectorAll(".pending-item")];
  const now = Date.now();

  for (const item of items) {
    const id = item.dataset.commandId || "?";
    if (!approvals.has(id)) approvals.set(id, now);
  }
  const live = new Set(items.map((i) => i.dataset.commandId || "?"));
  for (const id of [...approvals.keys()]) if (!live.has(id)) approvals.delete(id);

  const n = items.length;
  ui.approvals.hidden = n === 0;
  ui.navBadge.hidden = n === 0;
  if (ui.navBadge.textContent !== (n ? String(n) : "")) ui.navBadge.textContent = n ? String(n) : "";
  const incidentsNav = ui.navItems.find((b) => b.dataset.dest === "incidents");
  incidentsNav.setAttribute("aria-label", `Incidents${n ? `, ${n} approval${n === 1 ? "" : "s"}` : ""}`);

  if (!n) return;
  for (const item of items) if (item.parentElement !== ui.approvalItems) adopt(item, ui.approvalItems);

  const oldest = Math.min(...items.map((i) => approvals.get(i.dataset.commandId || "?") || now));
  const left = Math.max(0, APPROVAL_WINDOW_MS - (now - oldest));
  ui.approvalCount.textContent = `${n} approval${n === 1 ? "" : "s"} need you`;
  ui.approvalClock.textContent =
    left > 0
      ? `auto-denied in ${countdown(left)} · counted from when this tab first saw the request`
      : "auto-deny due · the controller denies a pending approval 300 s after it was issued";
}

function countdown(ms) {
  const total = Math.ceil(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/* ------------------------------------------------------------ small states
 * Empty and loading each get one line and one recovery action, never a blank
 * panel and never a red wall.
 */

function syncFleetState(cards) {
  const host = document.getElementById("overview");
  if (!host) return;
  const loading = !cards.length && !!host.querySelector(".skeleton");
  const empty = !cards.length && !!host.querySelector(".empty");
  ui.fleetState.hidden = false;
  ui.fleetState.replaceChildren();
  if (loading) {
    ui.fleetState.appendChild(el("span", { text: "Loading nodes…" }));
    return;
  }
  if (empty) {
    // The shipped empty panel already states the fact; this adds the recovery
    // action HIG requires (the variant adds no second sentence).
    const btn = el("button", { class: "hig-btn", type: "button", text: "Refresh" });
    btn.addEventListener("click", () => document.getElementById("btn-refresh")?.click());
    ui.fleetState.appendChild(btn);
    return;
  }
  ui.fleetState.hidden = true;
}

/* ------------------------------------------------------------- appearance */

function storedAppearance() {
  try {
    return localStorage.getItem(APPEARANCE_KEY) || "system";
  } catch {
    return "system";
  }
}

function setAppearance(mode, { persist = false } = {}) {
  state.appearance = ["system", "light", "dark"].includes(mode) ? mode : "system";
  const html = document.documentElement;
  if (state.appearance === "system") html.removeAttribute("data-hig-appearance");
  else html.dataset.higAppearance = state.appearance;
  for (const btn of ui.appearanceBtns || []) {
    const on = btn.dataset.appearance === state.appearance;
    btn.classList.toggle("hig-on", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }
  if (persist) {
    try {
      localStorage.setItem(APPEARANCE_KEY, state.appearance);
    } catch {
      /* a private window without storage is not a reason to fail a click */
    }
  }
}

/* ---------------------------------------------------------------- overlays */

/** Every form control needs an accessible name; `#intent-prompt` ships with a
 *  placeholder and no label, and a placeholder is not a name. The attribute is
 *  removed again on unmount so the shipped page is untouched. */
const labelled = [];

function labelFields() {
  for (const field of document.querySelectorAll("input, select, textarea")) {
    if (field.type === "hidden" || field.dataset.higName) continue;
    const hasLabel = (field.labels && field.labels.length) || field.getAttribute("aria-label") || field.getAttribute("aria-labelledby");
    if (!hasLabel) {
      const text = field.getAttribute("placeholder") || field.name || field.id || "field";
      field.setAttribute("aria-label", text.charAt(0).toUpperCase() + text.slice(1));
      labelled.push(field);
    }
    field.dataset.higName = "1";
  }
}

function openSheet() {
  if (!ui.sheet) return;
  const panel = document.getElementById("log-panel");
  if (panel) panel.open = true; // the surface exists to show the stream
  ui.sheet.showModal();
}

function closeMenus(except = null) {
  for (const card of document.querySelectorAll("#overview .node-card")) {
    const menu = card.querySelector(".hig-menu");
    const btn = card.querySelector(".hig-menu-btn");
    if (!menu || !btn || menu === except) continue;
    if (menu.hidden) continue;
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  }
}

function toggleMenu(card, menu, btn) {
  const open = menu.hidden;
  closeMenus(open ? menu : null);
  menu.hidden = !open;
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (!open) return;

  // Anchored under the button, clamped into the viewport (a row near the bottom
  // of the list must not open a menu that is half off-screen).
  const rect = btn.getBoundingClientRect();
  menu.style.visibility = "hidden";
  menu.hidden = false;
  const box = menu.getBoundingClientRect();
  const left = Math.min(window.innerWidth - box.width - 8, Math.max(8, rect.right - box.width));
  let top = rect.bottom + 6;
  if (top + box.height > window.innerHeight - 8) top = Math.max(8, rect.top - box.height - 6);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  menu.style.visibility = "";

  const first = menu.querySelector("button");
  first?.focus({ preventScroll: true });
  const step = (ev) => {
    const items = [...menu.querySelectorAll("button")].filter((b) => !b.disabled);
    const i = items.indexOf(document.activeElement);
    if (ev.key === "ArrowDown") items[(i + 1) % items.length]?.focus();
    else if (ev.key === "ArrowUp") items[(i - 1 + items.length) % items.length]?.focus();
    else if (ev.key === "Escape") {
      menu.hidden = true;
      btn.setAttribute("aria-expanded", "false");
      btn.focus();
    } else if (ev.key === "Tab") {
      menu.hidden = true;
      btn.setAttribute("aria-expanded", "false");
      return;
    } else return;
    ev.preventDefault();
  };
  menu.addEventListener("keydown", step);
}

/* ------------------------------------------------------------ keyboard fns */

function onListKeydown(ev) {
  const rows = listRows();
  if (!rows.length) return;
  const i = rows.findIndex((r) => r.dataset.decisionId === state.decisionId);
  let next = null;
  if (ev.key === "ArrowDown") next = rows[Math.min(rows.length - 1, i + 1)];
  else if (ev.key === "ArrowUp") next = rows[Math.max(0, i - 1)];
  else if (ev.key === "Home") next = rows[0];
  else if (ev.key === "End") next = rows[rows.length - 1];
  else if (ev.key === "Enter" && i >= 0) {
    ev.preventDefault();
    ui.decisionDetail.focus({ preventScroll: true });
    return;
  } else return;
  ev.preventDefault();
  if (next) selectDecision(next.dataset.decisionId);
}

function onGlobalKeydown(ev) {
  if (ev.key === "Escape") {
    const card = [...document.querySelectorAll("#overview .node-card")].find((c) => {
      const m = c.querySelector(".hig-menu");
      return m && !m.hidden;
    });
    if (card) {
      closeMenus();
      card.querySelector(".hig-menu-btn")?.focus();
      return;
    }
    if (document.documentElement.hasAttribute("data-hig-detail")) {
      document.documentElement.removeAttribute("data-hig-detail");
      ui.back.blur();
    }
  }
}

/** A click that is not on a menu or its trigger closes any open menu. */
function onDocumentClick(ev) {
  if (!ev.target.closest(".hig-menu, .hig-menu-btn")) closeMenus();
}

/* ------------------------------------------------------------------ mount */

export function mount() {
  if (mounted || document.documentElement.dataset.variant !== VARIANT) return;
  mounted = true;

  buildShell();
  setAppearance(new URLSearchParams(location.search).get("appearance") || storedAppearance());

  const dest = new URLSearchParams(location.search).get("dest");
  go(DESTINATIONS.includes(dest) ? dest : "fleet");

  document.getElementById("decision-list")?.addEventListener("keydown", onListKeydown);
  document.addEventListener("keydown", onGlobalKeydown);
  document.addEventListener("click", onDocumentClick);

  reconcile();
  // The shipped panels re-render on their own polls (overview every 3 s, the
  // incident list on every decision poll). This module does not own that data:
  // it reconciles the shape afterwards, on a cheap timer, instead of hooking
  // observers into code it must not modify.
  sweep = setInterval(reconcile, RECONCILE_MS);
  clock = setInterval(() => {
    if (!ui.approvals || ui.approvals.hidden) return;
    const items = [...ui.approvalItems.querySelectorAll(".pending-item")];
    if (!items.length) return;
    const now = Date.now();
    const oldest = Math.min(...items.map((i) => approvals.get(i.dataset.commandId || "?") || now));
    const left = Math.max(0, APPROVAL_WINDOW_MS - (now - oldest));
    ui.approvalClock.textContent =
      left > 0
        ? `auto-denied in ${countdown(left)} · counted from when this tab first saw the request`
        : "auto-deny due · the controller denies a pending approval 300 s after it was issued";
  }, 1000);
}

export function unmount() {
  if (!mounted) return;
  mounted = false;
  clearInterval(sweep);
  clearInterval(clock);
  sweep = null;
  clock = null;
  document.getElementById("decision-list")?.removeEventListener("keydown", onListKeydown);
  document.removeEventListener("keydown", onGlobalKeydown);
  document.removeEventListener("click", onDocumentClick);

  if (ui.sheet && ui.sheet.open) ui.sheet.close();
  restore();
  if (shell) shell.remove();
  shell = null;
  document.documentElement.removeAttribute("data-hig-dest");
  document.documentElement.removeAttribute("data-hig-detail");
  document.documentElement.removeAttribute("data-hig-appearance");

  for (const node of document.querySelectorAll("[data-hig-row], [data-hig-option], [data-hig-layout]")) {
    node.removeAttribute("data-hig-row");
    node.removeAttribute("data-hig-option");
    node.removeAttribute("data-hig-layout");
    node.classList.remove("hig-row");
    if (node.hasAttribute("role") && node.getAttribute("role") === "option") node.removeAttribute("role");
    node.removeAttribute("aria-selected");
    node.removeAttribute("aria-current");
    node.removeAttribute("tabindex");
  }
  for (const list of [document.getElementById("decision-list")]) {
    list?.removeAttribute("role");
    list?.removeAttribute("aria-label");
  }
  for (const field of labelled) field.removeAttribute("aria-label");
  labelled.length = 0;
  for (const field of document.querySelectorAll("[data-hig-name]")) field.removeAttribute("data-hig-name");
  approvals.clear();
  state.decisionId = null;
  for (const key of Object.keys(ui)) delete ui[key];
}

/* -------------------------------------------------------------- lifecycle
 * The harness announces every picker change. The module also mounts itself if
 * the page was loaded with this variant already selected (a reload of ?v=4,
 * or the initial paint), because module scripts run after harness.js.
 */
window.addEventListener("proto:variant-change", (ev) => {
  const id = ev && ev.detail ? ev.detail.variant : null;
  if (id === VARIANT) mount();
  else unmount();
});

function reconcile() {
  if (!mounted) return;
  owned = owned.filter((n) => typeof n.remove !== "function" || n.isConnected !== false);
  labelFields();
  decorateCards();
  placeNodeDetail();
  decorateDecisions();
  placeDecisionDetail();
  syncApprovals();
  syncSubtitle();
}

if (document.documentElement.dataset.variant === VARIANT) mount();
