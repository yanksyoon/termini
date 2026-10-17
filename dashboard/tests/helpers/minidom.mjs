/*
 * A minimal, dependency-free DOM for the UI tests.
 *
 * The dashboard is a dependency-free static bundle (no jsdom, no framework), so
 * its UI tests get the same treatment: this file implements just enough of the
 * DOM for `js/ui/*` to render into — element tree, attributes, classList,
 * dataset, `textContent`, event listeners, `document.activeElement` and a
 * `<details>` `open` property that fires `toggle` the way a browser does
 * (synchronously, so a test can drive a user collapse without timers).
 *
 * Deliberately NOT implemented: layout (no offsets, no scroll metrics), CSS,
 * form semantics, shadow DOM. Tests assert on structure and state only.
 */

const ELEMENT_NODE = 1;
const TEXT_NODE = 3;
const FRAGMENT_NODE = 11;

let doc = null;

class TextNode {
  constructor(text) {
    this.nodeType = TEXT_NODE;
    this.data = String(text);
    this.parentNode = null;
  }

  get textContent() {
    return this.data;
  }

  set textContent(value) {
    this.data = String(value);
  }

  _detach() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
}

class Element {
  constructor(tag) {
    this.nodeType = ELEMENT_NODE;
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.dataset = {};
    this.hidden = false;
    this._listeners = new Map();
  }

  /* ---------------------------------------------------------- tree shape */

  get children() {
    return this.childNodes.filter((n) => n.nodeType === ELEMENT_NODE);
  }

  get parentElement() {
    return this.parentNode && this.parentNode.nodeType === ELEMENT_NODE ? this.parentNode : null;
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  get nextSibling() {
    if (!this.parentNode) return null;
    const list = this.parentNode.childNodes;
    const i = list.indexOf(this);
    return i === -1 ? null : list[i + 1] || null;
  }

  appendChild(node) {
    if (!node) return node;
    if (node.nodeType === FRAGMENT_NODE) {
      for (const child of [...node.childNodes]) this.appendChild(child);
      return node;
    }
    node._detach();
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  append(...nodes) {
    for (const n of nodes) this.appendChild(n);
  }

  insertBefore(node, ref) {
    if (!ref) return this.appendChild(node);
    node._detach();
    node.parentNode = this;
    this.childNodes.splice(this.childNodes.indexOf(ref), 0, node);
    return node;
  }

  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i !== -1) this.childNodes.splice(i, 1);
    node.parentNode = null;
    if (doc) doc._forgetFocus(node);
    return node;
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  _detach() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  /* -------------------------------------------------------- text/content */

  get textContent() {
    return this.childNodes.map((n) => n.textContent).join("");
  }

  set textContent(value) {
    for (const child of [...this.childNodes]) this.removeChild(child);
    if (value !== "" && value !== null && value !== undefined) this.appendChild(new TextNode(value));
  }

  /* ---------------------------------------------------------- attributes */

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name.startsWith("data-")) {
      const key = name
        .slice(5)
        .replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = String(value);
    }
    if (name === "open") this.open = true;
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  get className() {
    return this.getAttribute("class") || "";
  }

  set className(value) {
    this.attributes.set("class", String(value));
  }

  get classList() {
    const self = this;
    const read = () => self.className.split(/\s+/).filter(Boolean);
    const write = (list) => {
      self.className = [...new Set(list)].join(" ");
    };
    return {
      add(...names) {
        write([...read(), ...names]);
      },
      remove(...names) {
        write(read().filter((c) => !names.includes(c)));
      },
      contains(name) {
        return read().includes(name);
      },
      toggle(name, force) {
        const on = force === undefined ? !read().includes(name) : !!force;
        if (on) this.add(name);
        else this.remove(name);
        return on;
      },
    };
  }

  get id() {
    return this.getAttribute("id") || "";
  }

  set id(value) {
    this.setAttribute("id", value);
  }

  /**
   * `<details open>` — the property reflects the attribute, and changing it
   * fires `toggle` on the element. A browser queues that event; firing it
   * synchronously keeps the test free of timers, and the panel's handler only
   * reads `open`, so the two are equivalent here.
   */
  get open() {
    return this.hasAttribute("open");
  }

  set open(value) {
    const next = !!value;
    if (next === this.hasAttribute("open")) return;
    if (next) this.attributes.set("open", "");
    else this.attributes.delete("open");
    this.dispatchEvent({ type: "toggle" });
  }

  /* --------------------------------------------------------------- events */

  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }

  removeEventListener(type, fn) {
    const list = this._listeners.get(type) || [];
    this._listeners.set(type, list.filter((f) => f !== fn));
  }

  dispatchEvent(event) {
    const ev = Object.assign({ type: "", target: this }, event);
    for (const fn of [...(this._listeners.get(ev.type) || [])]) fn(ev);
    return true;
  }

  /**
   * Focus is modelled as "the last element asked to take it". Detaching that
   * element drops focus back to `document.body`, exactly like a browser, so a
   * teardown-and-rebuild render loses focus and a preserved row does not.
   */
  focus() {
    if (doc) doc.activeElement = this;
  }

  blur() {
    if (doc && doc.activeElement === this) doc.activeElement = doc.body;
  }

  /* ------------------------------------------------------------ selection */

  querySelectorAll(selector) {
    const groups = String(selector)
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const out = [];
    for (const node of this._descendants()) {
      if (groups.some((g) => matchesChain(node, this, g.split(/\s+/).filter(Boolean)))) out.push(node);
    }
    return out;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  _descendants() {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}

class DocumentFragment {
  constructor() {
    this.nodeType = FRAGMENT_NODE;
    this.childNodes = [];
    this.parentNode = null;
  }

  get children() {
    return this.childNodes.filter((n) => n.nodeType === ELEMENT_NODE);
  }

  appendChild(node) {
    node._detach();
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i !== -1) this.childNodes.splice(i, 1);
    node.parentNode = null;
    return node;
  }
}

/* ------------------------------------------------------------ selectors */

function matchesCompound(node, token) {
  const m = /^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)((?:\[[^\]]+\])*)$/.exec(token);
  if (!m) return false;
  const [, tag, classesAndIds, attrs] = m;
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  const parts = classesAndIds.match(/[.#][\w-]+/g) || [];
  for (const part of parts) {
    if (part[0] === "." && !node.classList.contains(part.slice(1))) return false;
    if (part[0] === "#" && node.id !== part.slice(1)) return false;
  }
  const attrTokens = attrs.match(/\[[^\]]+\]/g) || [];
  for (const token2 of attrTokens) {
    const body = token2.slice(1, -1);
    const eq = body.indexOf("=");
    if (eq === -1) {
      if (!node.hasAttribute(body)) return false;
    } else {
      const name = body.slice(0, eq);
      const want = body.slice(eq + 1).replace(/^["']|["']$/g, "");
      if (node.getAttribute(name) !== want) return false;
    }
  }
  return true;
}

/** Right-to-left match of a descendant chain (`ol li details.foo`). */
function matchesChain(node, root, tokens) {
  if (!tokens.length) return false;
  if (!matchesCompound(node, tokens[tokens.length - 1])) return false;
  let index = tokens.length - 2;
  let parent = node.parentElement;
  while (index >= 0 && parent && parent !== root) {
    if (matchesCompound(parent, tokens[index])) index -= 1;
    parent = parent.parentElement;
  }
  return index < 0;
}

/* ------------------------------------------------------------- document */

class Document {
  constructor() {
    this.body = new Element("body");
    this.documentElement = new Element("html");
    this.visibilityState = "visible";
    this._activeElement = this.body;
  }

  get activeElement() {
    return this._activeElement;
  }

  set activeElement(node) {
    this._activeElement = node || this.body;
  }

  _forgetFocus(removed) {
    let node = this._activeElement;
    while (node) {
      if (node === removed) {
        this._activeElement = this.body;
        return;
      }
      node = node.parentNode;
    }
  }

  createElement(tag) {
    return new Element(tag);
  }

  createTextNode(text) {
    return new TextNode(text);
  }

  createDocumentFragment() {
    return new DocumentFragment();
  }

  querySelector(selector) {
    return this.body.querySelector(selector);
  }

  querySelectorAll(selector) {
    return this.body.querySelectorAll(selector);
  }
}

/**
 * Install a fresh mini-DOM as `globalThis.document` (and a no-op
 * `globalThis.window`). Returns the document so a test can build hosts.
 */
export function installDom() {
  doc = new Document();
  globalThis.document = doc;
  globalThis.window = globalThis.window || { document: doc };
  return doc;
}

export { ELEMENT_NODE, TEXT_NODE, FRAGMENT_NODE };
