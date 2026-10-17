/*
 * Repository accessibility check — the axe critical-impact rule set, without the
 * dependency.
 *
 * axe-core could not be installed in this environment (the npm package threat
 * intelligence lookup is unavailable to the sandbox), so this implements the
 * rules axe reports at impact "critical" — plus colour contrast, which axe
 * reports as "serious" and which the card's acceptance names separately. Each
 * rule names the axe rule it mirrors, so a reader can compare the two.
 *
 * Evaluated in the page over CDP; returns { violations: [...], checked: [...] }.
 */
(() => {
  const violations = [];
  const checked = [];

  const visible = (el) => {
    if (!el || el.nodeType !== 1) return false;
    if (el.closest("[hidden]")) return false;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0.5 && r.height > 0.5;
  };
  const path = (el) => {
    const bits = [];
    let n = el;
    while (n && n.nodeType === 1 && bits.length < 4) {
      let bit = n.tagName.toLowerCase();
      if (n.id) bit += "#" + n.id;
      else if (n.classList.length) bit += "." + [...n.classList].slice(0, 2).join(".");
      bits.unshift(bit);
      n = n.parentElement;
    }
    return bits.join(" > ");
  };
  const report = (rule, impact, nodes, message) => {
    if (!nodes.length) return;
    violations.push({
      rule,
      impact,
      message,
      count: nodes.length,
      nodes: nodes.slice(0, 8).map((n) => ({ target: path(n), text: (n.textContent || n.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ").slice(0, 60) })),
    });
  };
  const textOf = (el) =>
    (el.getAttribute("aria-label") || "").trim() ||
    (() => {
      const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
      return ids.map((id) => document.getElementById(id)?.textContent?.trim() || "").join(" ").trim();
    })() ||
    // Implicit and explicit <label> association, as axe's `label` rule accepts.
    (el.labels && el.labels.length
      ? [...el.labels].map((l) => l.textContent || "").join(" ").trim()
      : "") ||
    (el.tagName === "INPUT" ? (el.value || "").trim() : el.textContent.trim()) ||
    (el.getAttribute("title") || "").trim();

  /* ----------------------------------------------------- document-level */
  checked.push("html-has-lang");
  if (!document.documentElement.getAttribute("lang")) {
    report("html-has-lang", "serious", [document.documentElement], "The <html> element must have a lang attribute");
  }
  checked.push("document-title");
  if (!document.title.trim()) {
    report("document-title", "serious", [document.documentElement], "The document must have a title");
  }
  checked.push("aria-hidden-body");
  if (document.body.getAttribute("aria-hidden") === "true") {
    report("aria-hidden-body", "critical", [document.body], "aria-hidden must not be set on <body>");
  }

  /* --------------------------------------------------------- duplicate ids */
  checked.push("duplicate-id-aria");
  const ids = new Map();
  for (const el of document.querySelectorAll("[id]")) ids.set(el.id, (ids.get(el.id) || 0) + 1);
  const dupes = [...ids.entries()].filter(([, n]) => n > 1).map(([id]) => id);
  if (dupes.length) {
    const holders = dupes.map((id) => document.getElementById(id)).filter(Boolean);
    report("duplicate-id-aria", "critical", holders, `Duplicate ids: ${dupes.slice(0, 8).join(", ")}`);
  }

  /* ---------------------------------------------------------------- names */
  const nameRule = (rule, selector, impact, label) => {
    checked.push(rule);
    const bad = [...document.querySelectorAll(selector)].filter((el) => visible(el) && !textOf(el));
    if (bad.length) report(rule, impact, bad, label);
  };
  nameRule("button-name", "button, [role=button], input[type=button], input[type=submit], input[type=reset]", "critical", "Buttons need discernible text");
  nameRule("link-name", "a[href]", "serious", "Links need discernible text");
  nameRule("select-name", "select", "critical", "Selects need an accessible name");
  nameRule("label", "input:not([type=hidden]), textarea", "critical", "Form fields need a label");

  checked.push("image-alt");
  const imgs = [...document.querySelectorAll("img")].filter((img) => !img.hasAttribute("alt") && !img.getAttribute("role"));
  if (imgs.length) report("image-alt", "critical", imgs, "Images need alt text or role=presentation");

  /* --------------------------------------------------------------- ARIA */
  const ROLES = new Set([
    "alert", "alertdialog", "application", "article", "banner", "button", "cell", "checkbox", "columnheader", "combobox",
    "complementary", "contentinfo", "dialog", "document", "feed", "figure", "form", "grid", "gridcell", "group", "heading",
    "img", "link", "list", "listbox", "listitem", "log", "main", "marquee", "math", "menu", "menubar", "menuitem",
    "menuitemcheckbox", "menuitemradio", "navigation", "none", "note", "option", "presentation", "progressbar", "radio",
    "radiogroup", "region", "row", "rowgroup", "rowheader", "scrollbar", "search", "searchbox", "separator", "slider",
    "spinbutton", "status", "switch", "tab", "table", "tablist", "tabpanel", "term", "textbox", "timer", "toolbar",
    "tooltip", "tree", "treegrid", "treeitem",
  ]);
  checked.push("aria-roles");
  const badRoles = [...document.querySelectorAll("[role]")].filter((el) =>
    el.getAttribute("role").split(/\s+/).some((r) => r && !ROLES.has(r)),
  );
  if (badRoles.length) report("aria-roles", "critical", badRoles, "Unknown ARIA role");

  checked.push("aria-required-attr");
  const REQ = { listbox: [], option: ["aria-selected"], menu: [], menuitem: [], dialog: [], checkbox: ["aria-checked"], switch: ["aria-checked"], slider: ["aria-valuenow"] };
  const missingAttr = [];
  for (const el of document.querySelectorAll("[role]")) {
    const need = REQ[el.getAttribute("role")];
    if (need && need.some((a) => !el.hasAttribute(a))) missingAttr.push(el);
  }
  if (missingAttr.length) report("aria-required-attr", "critical", missingAttr, "Role is missing a required state/property");

  checked.push("aria-required-parent");
  const orphan = [];
  for (const el of document.querySelectorAll("[role=option]")) {
    if (!el.closest("[role=listbox]")) orphan.push(el);
  }
  for (const el of document.querySelectorAll("[role=menuitem]")) {
    if (!el.closest("[role=menu], [role=menubar]")) orphan.push(el);
  }
  if (orphan.length) report("aria-required-parent", "critical", orphan, "Role is not inside its required parent");

  checked.push("aria-required-children");
  const childless = [];
  for (const list of document.querySelectorAll("[role=listbox]")) {
    if (!list.querySelector("[role=option]") && list.children.length) childless.push(list);
  }
  for (const menu of document.querySelectorAll("[role=menu]")) {
    if (!menu.querySelector("[role=menuitem]") && menu.children.length) childless.push(menu);
  }
  if (childless.length) report("aria-required-children", "critical", childless, "Container role has children that are not the required role");

  checked.push("nested-interactive");
  // axe's nested-interactive rule is about WIDGETS inside widgets. A scrollable
  // region (`div[tabindex=0]`) or a focusable `role=region` pane is not a
  // widget, and treating it as one reported 45 false positives on this page
  // while a real defect — buttons inside a `role=option` row — hid among them.
  const WIDGET = [
    "button", "a[href]", "input", "select", "textarea", "summary",
    "[role=button]", "[role=link]", "[role=checkbox]", "[role=switch]", "[role=radio]",
    "[role=tab]", "[role=menuitem]", "[role=menuitemcheckbox]", "[role=menuitemradio]",
    "[role=option]", "[role=treeitem]", "[role=combobox]", "[role=slider]", "[role=spinbutton]",
  ].join(", ");
  const nested = [...document.querySelectorAll(WIDGET)].filter(
    (el) => !el.matches("html, body") && el.parentElement && el.parentElement.closest(WIDGET) && visible(el),
  );
  if (nested.length) report("nested-interactive", "serious", nested, "Interactive control is nested inside another interactive control");

  /* -------------------------------------------------------- colour contrast
   * WCAG 2.x relative luminance on composited backgrounds; the same maths the
   * measurement sweep uses, so the two reports cannot disagree.
   */
  const rgb = (value) => {
    const m = String(value).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const over = (fg, bg) => ({ r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 });
  const lum = (c) => {
    const f = (v) => ((v /= 255), v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const ratio = (a, b) => {
    const l1 = lum(a) + 0.05;
    const l2 = lum(b) + 0.05;
    return Math.max(l1, l2) / Math.min(l1, l2);
  };
  const bgOf = (el) => {
    const chain = [];
    let n = el;
    while (n && n.nodeType === 1) {
      chain.push(n);
      n = n.parentElement;
    }
    let base = rgb(getComputedStyle(document.body).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 };
    if (base.a < 1) base = { r: 255, g: 255, b: 255, a: 1 };
    for (const node of chain.reverse()) {
      const c = rgb(getComputedStyle(node).backgroundColor);
      if (c && c.a > 0) base = over(c, base);
    }
    return base;
  };
  checked.push("color-contrast");
  const lowContrast = [];
  for (const el of document.querySelectorAll("body *")) {
    if (!visible(el)) continue;
    if (el.closest(".proto-picker, .proto-caption, script, style, noscript")) continue;
    const hasText = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim().length > 1);
    if (!hasText) continue;
    const cs = getComputedStyle(el);
    const fg = rgb(cs.color);
    const bg = bgOf(el);
    if (!fg) continue;
    const r = ratio(over(fg, bg), bg);
    const size = parseFloat(cs.fontSize);
    const large = size >= 24 || (size >= 18.66 && Number(cs.fontWeight) >= 600);
    if (r < (large ? 3 : 4.5)) lowContrast.push(el);
  }
  if (lowContrast.length) report("color-contrast", "serious", lowContrast, "Text does not meet WCAG AA contrast");

  /* --------------------------------------------------------------- summary */
  return {
    url: location.href,
    variant: document.documentElement.dataset.variant,
    destination: document.documentElement.dataset.higDest || null,
    rulesChecked: checked,
    violations: violations.map((v) => ({ ...v, impact: v.impact })),
    critical: violations.filter((v) => v.impact === "critical").length,
    serious: violations.filter((v) => v.impact === "serious").length,
  };
})();
