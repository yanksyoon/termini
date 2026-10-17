/*
 * The measurement half of the HIG acceptance, evaluated in the page over CDP.
 *
 * Everything the card asks to be MEASURED rather than asserted is computed here
 * from the live layout: viewport fit, control count, 44 px targets, icon-only
 * labels, the type scale and weights, every distinct text colour, and the WCAG
 * contrast ratio of every text run against its composited background.
 *
 * Returns a JSON-serialisable object; tools/audit.py writes it to disk.
 */
(() => {
  const visible = (el) => {
    if (!el || el.nodeType !== 1) return false;
    if (el.closest("[hidden]")) return false;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || Number(cs.opacity) === 0) return false;
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

  const rgb = (value) => {
    const m = String(value).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const parts = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const over = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a),
    a: 1,
  });
  const lum = (c) => {
    const f = (v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const ratio = (a, b) => {
    const l1 = lum(a) + 0.05;
    const l2 = lum(b) + 0.05;
    return Math.round((Math.max(l1, l2) / Math.min(l1, l2)) * 100) / 100;
  };
  const backgroundOf = (el) => {
    const chain = [];
    let n = el;
    while (n && n.nodeType === 1) {
      chain.push(n);
      n = n.parentElement;
    }
    let base = { r: 255, g: 255, b: 255, a: 1 };
    const htmlBg = rgb(getComputedStyle(document.documentElement).backgroundColor);
    if (htmlBg && htmlBg.a > 0) base = htmlBg;
    const bodyBg = rgb(getComputedStyle(document.body).backgroundColor);
    if (bodyBg && bodyBg.a > 0) base = bodyBg;
    for (const node of chain.reverse()) {
      const c = rgb(getComputedStyle(node).backgroundColor);
      if (c && c.a > 0) base = over(c, base);
    }
    return base;
  };

  /* ---------------------------------------------------------- viewport fit */
  const docEl = document.documentElement;
  const out = {
    url: location.href,
    title: document.title,
    variant: docEl.dataset.variant,
    destination: docEl.dataset.higDest || null,
    appearance: docEl.dataset.higAppearance || "system(prefers-color-scheme)",
    prefersDark: matchMedia("(prefers-color-scheme: dark)").matches,
    viewport: { w: innerWidth, h: innerHeight },
    doc: {
      scrollWidth: docEl.scrollWidth,
      clientWidth: docEl.clientWidth,
      scrollHeight: docEl.scrollHeight,
      clientHeight: docEl.clientHeight,
    },
    horizontalOverflow: docEl.scrollWidth > docEl.clientWidth + 1,
    controls: [],
    under44: [],
    iconOnlyUnlabelled: [],
    fieldsUnnamed: [],
    fontSizes: {},
    fontSizesBySelector: {},
    fontWeights: {},
    textColors: {},
    contrast: [],
    contrastWorst: [],
    chromeCount: 0,
  };

  /* -------------------------------------------------------------- controls */
  const CONTROL_SEL =
    "button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=menuitem], [role=option][tabindex='0']";

  /* The accessible name, the way axe computes it for the label/button-name
     rules: aria-label, aria-labelledby, a `<label for>` or a wrapping
     `<label>`, otherwise the element's own text. A field that is named by its
     visible label is NOT an unlabelled icon-only control, and it does satisfy
     the repo a11y check's `label` rule — both are checked against this
     function so the two reports cannot disagree. */
  const labelFor = (el) => {
    if (el.labels && el.labels.length) {
      return [...el.labels].map((l) => l.textContent || "").join(" ").trim();
    }
    const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
    if (ids.length) {
      const text = ids.map((id) => document.getElementById(id)?.textContent || "").join(" ").trim();
      if (text) return text;
    }
    return (el.getAttribute("aria-label") || "").trim();
  };
  const accessibleName = (el) =>
    labelFor(el) || (el.tagName === "INPUT" ? (el.value || "").trim() : (el.textContent || "").trim()) || (el.getAttribute("title") || "").trim();

  const controls = [...document.querySelectorAll(CONTROL_SEL)].filter(visible);
  const seen = new Set();
  for (const el of controls) {
    if (seen.has(el)) continue;
    seen.add(el);
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const label = (el.getAttribute("aria-label") || el.textContent || el.value || "").trim().replace(/\s+/g, " ").slice(0, 48);
    const picker = !!el.closest(".proto-picker, .proto-caption");
    const entry = {
      tag: el.tagName.toLowerCase(),
      sel: path(el),
      label,
      w: Math.round(r.width * 10) / 10,
      h: Math.round(r.height * 10) / 10,
      font: cs.fontSize,
      picker,
      type: el.getAttribute("type") || "",
      row: !!el.closest(".node-card") || !!el.closest(".decision-row"),
      inSidebar: !!el.closest(".hig-sidebar"),
      inBar: !!el.closest(".hig-bar"),
      inToolbar: !!el.closest(".panel-head"),
      inBanner: !!el.closest(".hig-approvals, .banner"),
    };
    out.controls.push(entry);
    if (!picker && entry.type !== "checkbox" && (r.height < 43.5 || r.width < 43.5)) out.under44.push(entry);
    // "Icon-only" is a control with no visible text AND no accessible name. A
    // text field named by its wrapping `<label>` is neither.
    const noText = !(el.textContent || "").trim() && !["input", "select", "textarea"].includes(el.tagName.toLowerCase());
    const named = !!accessibleName(el) || !!el.getAttribute("title");
    if (!picker && noText && !named) {
      out.iconOnlyUnlabelled.push(entry);
    }
    // A field with no name at all is a different, worse defect; report it too.
    if (!picker && ["input", "select", "textarea"].includes(el.tagName.toLowerCase()) && !accessibleName(el)) {
      out.fieldsUnnamed.push(entry);
    }
  }
  // "Chrome" = the persistent page furniture: the sidebar, the destination bar,
  // a panel's own toolbar and a banner. A row's single action, a chart's own
  // "Ask AI", a form field and a detail-pane link are CONTENT, not chrome — the
  // acceptance excludes the row action explicitly and asks for ≤20 chrome
  // controls per screen. Both numbers are reported so the definition cannot be
  // mistaken for a thumb on the scale.
  out.chromeCount = out.controls.filter(
    (c) => !c.picker && (c.inSidebar || c.inBar || c.inToolbar || c.inBanner) && !c.row,
  ).length;
  out.contentControls = out.controls.filter((c) => !c.picker && !c.row).length;

  /* ------------------------------------------------------------------ type */
  const textEls = [...document.querySelectorAll("body *")].filter((el) => {
    if (!visible(el)) return false;
    if (el.closest(".proto-picker, .proto-caption, script, style")) return false;
    return [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim().length > 1);
  });
  for (const el of textEls) {
    const cs = getComputedStyle(el);
    const sample = [...el.childNodes]
      .filter((n) => n.nodeType === 3 && n.textContent.trim())
      .map((n) => n.textContent.trim())
      .join(" ")
      .slice(0, 40);
    const size = Math.round(parseFloat(cs.fontSize) * 10) / 10;
    const weight = cs.fontWeight;
    const color = cs.color;
    out.fontSizes[size] = (out.fontSizes[size] || 0) + 1;
    if (!out.fontSizesBySelector[size]) out.fontSizesBySelector[size] = [];
    const sel = path(el);
    if (out.fontSizesBySelector[size].length < 8 && !out.fontSizesBySelector[size].includes(sel)) {
      out.fontSizesBySelector[size].push(sel);
    }
    out.fontWeights[weight] = (out.fontWeights[weight] || 0) + 1;
    out.textColors[color] = (out.textColors[color] || 0) + 1;

    const fg = rgb(color);
    const bg = backgroundOf(el);
    if (!fg || !bg) continue;
    const r = ratio(over(fg, bg), bg);
    const large = size >= 24 || (size >= 18.66 && Number(weight) >= 600);
    const need = large ? 3 : 4.5;
    const row = { sel: path(el), text: sample, color, bg: `rgb(${Math.round(bg.r)}, ${Math.round(bg.g)}, ${Math.round(bg.b)})`, size, weight, ratio: r, need };
    out.contrast.push(row);
    if (r < need) out.contrastWorst.push(row);
  }
  out.contrastWorst.sort((a, b) => a.ratio - b.ratio);
  out.contrastBelowTotal = out.contrastWorst.length;
  out.contrastWorst = out.contrastWorst.slice(0, 25);
  out.contrastChecked = out.contrast.length;
  delete out.contrast;

  /* ------------------------------------------------------------- structure */
  out.destinations = [...document.querySelectorAll(".hig-nav-item")].map((b) => ({
    label: b.textContent.trim(),
    current: b.getAttribute("aria-current"),
    h: Math.round(b.getBoundingClientRect().height),
  }));
  out.listboxes = [...document.querySelectorAll("[role=listbox]")].length;
  out.options = [...document.querySelectorAll("[role=option]")].length;
  out.selectedOptions = [...document.querySelectorAll("[role=option][aria-selected=true]")].length;
  const active = document.activeElement;
  out.activeElement = active ? (active.id || active.className || active.tagName) : null;
  out.sheetOpen = !!document.querySelector("dialog[open]");

  return out;
})();
