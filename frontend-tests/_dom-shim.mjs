/*
 * Minimal DOM shim for the PassOrch frontend tests.
 *
 * WHY THIS EXISTS
 * The frontend is browser ES modules that call a small, well-defined DOM
 * subset. This shim implements exactly that subset - and nothing else - so the
 * REAL modules in `frontend/js/` can be imported and executed by Node.js with
 * no dependencies (no jsdom, no browser download).
 *
 * It is test-only harness code. It is never imported by the application.
 *
 * SUPPORTED SUBSET (verified against the actual source usage)
 *   document.createElement, document.createTextNode,
 *   document.getElementById, document.querySelector, document.querySelectorAll
 *   Node tree: parentNode, childNodes, firstChild, children, appendChild,
 *   removeChild, remove, textContent (recursive, real semantics)
 *   Element: tagName, className, id, setAttribute/getAttribute/removeAttribute,
 *   addEventListener/dispatchEvent, value, selected, dataset
 *   window.location, window.addEventListener/removeEventListener
 *   Node as a common base of Element and Text (so `instanceof Node` works)
 *
 * DELIBERATELY NOT IMPLEMENTED
 *   CSS / layout, focus, forms, scrolling, real event bubbling/capture,
 *   Namespace handling, MutationObserver, any rendering. None of these are
 *   used by the application, so implementing them would be speculation.
 *
 * SAFETY
 * `innerHTML`, `outerHTML` and `insertAdjacentHTML` THROW on use, so if a view
 * ever reaches for an HTML-parsing sink the test fails immediately instead of
 * silently "passing" because the shim accepted the assignment.
 */

/* ---- node types ------------------------------------------------------------ */

export const ELEMENT_NODE = 1;
export const TEXT_NODE = 3;

/** Common base so `x instanceof Node` is true for elements and text alike. */
export class ShimNode {
  constructor(nodeType) {
    this.nodeType = nodeType;
    this.childNodes = [];
    this.parentNode = null;
  }

  get firstChild() {
    return this.childNodes.length > 0 ? this.childNodes[0] : null;
  }

  get children() {
    return this.childNodes.filter((n) => n.nodeType === ELEMENT_NODE);
  }

  appendChild(node) {
    if (!(node instanceof ShimNode)) {
      throw new TypeError("appendChild requires a Node");
    }
    // Real DOM semantics: appending a node removes it from its old parent.
    if (node.parentNode) {
      node.parentNode.removeChild(node);
    }
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i === -1) {
      throw new Error("NotFoundError: node is not a child of this node");
    }
    this.childNodes.splice(i, 1);
    node.parentNode = null;
    return node;
  }

  remove() {
    if (this.parentNode) {
      this.parentNode.removeChild(this);
    }
  }

  get textContent() {
    return this.childNodes.map((c) => c.textContent).join("");
  }

  set textContent(value) {
    for (const child of this.childNodes.slice()) {
      this.removeChild(child);
    }
    if (value !== "" && value !== null && value !== undefined) {
      this.appendChild(new ShimText(String(value)));
    }
  }
}

export class ShimText extends ShimNode {
  constructor(data) {
    super(TEXT_NODE);
    this.data = data === undefined || data === null ? "" : String(data);
  }

  get textContent() {
    return this.data;
  }

  set textContent(value) {
    this.data = String(value);
  }
}

export class ShimElement extends ShimNode {
  constructor(tagName) {
    super(ELEMENT_NODE);
    this.tagName = String(tagName || "").toUpperCase();
    this.className = "";
    this._id = "";
    this.attrs = new Map();
    this.listeners = new Map();
    this.dataset = {};
    this._value = "";
    this._selected = false;
    this.ownerDocument = null;
  }

  get id() {
    return this._id;
  }

  set id(value) {
    this._id = String(value);
    this.setAttribute("id", String(value));
  }

  setAttribute(name, value) {
    const key = String(name);
    this.attrs.set(key, String(value));
    if (key === "id") {
      this._id = String(value);
    }
    if (key === "class") {
      this.className = String(value);
    }
    // `selected` is a reflected property, as in the browser.
    if (key === "selected") {
      this._selected = true;
    }
  }

  getAttribute(name) {
    const key = String(name);
    return this.attrs.has(key) ? this.attrs.get(key) : null;
  }

  removeAttribute(name) {
    const key = String(name);
    this.attrs.delete(key);
    if (key === "class") {
      this.className = "";
    }
  }

  hasAttribute(name) {
    return this.attrs.has(String(name));
  }

  /** Search this element's subtree (and itself) for selector matches. */
  querySelectorAll(selector) {
    return this.ownerDocument ? this.ownerDocument.querySelectorAll(selector, this) : [];
  }

  querySelector(selector) {
    const all = this.querySelectorAll(selector);
    return all.length > 0 ? all[0] : null;
  }

  addEventListener(type, handler) {
    if (typeof handler !== "function") {
      return;
    }
    if (!this.listeners.has(type)) {
      this.listeners.set(type, []);
    }
    this.listeners.get(type).push(handler);
  }

  removeEventListener(type, handler) {
    const list = this.listeners.get(type);
    if (!list) {
      return;
    }
    const i = list.indexOf(handler);
    if (i >= 0) {
      list.splice(i, 1);
    }
  }

  /** Synchronously invoke every handler registered for `type`. */
  dispatchEvent(event) {
    const list = this.listeners.get(event.type) || [];
    for (const handler of list.slice()) {
      handler(event);
    }
    return true;
  }

  get value() {
    if (this.tagName !== "SELECT") {
      return this._value;
    }
    // A <select> reports the value of its selected option, as the browser does.
    for (const child of this.childNodes) {
      if (child.nodeType === ELEMENT_NODE && child._selected) {
        return child.getAttribute("value") || "";
      }
    }
    return "";
  }

  set value(value) {
    if (this.tagName === "SELECT") {
      const wanted = String(value);
      for (const child of this.childNodes) {
        if (child.nodeType === ELEMENT_NODE) {
          child._selected = child.getAttribute("value") === wanted;
        }
      }
      return;
    }
    this._value = value === undefined || value === null ? "" : String(value);
  }

  get selected() {
    return this._selected;
  }

  set selected(value) {
    this._selected = Boolean(value);
  }

  /* ---- unsafe sinks: fail loudly ----------------------------------------- */

  set innerHTML(_v) {
    throw new Error("UNSAFE SINK: innerHTML assignment is forbidden");
  }

  get innerHTML() {
    throw new Error("UNSAFE SINK: reading innerHTML is forbidden");
  }

  set outerHTML(_v) {
    throw new Error("UNSAFE SINK: outerHTML assignment is forbidden");
  }

  get outerHTML() {
    throw new Error("UNSAFE SINK: reading outerHTML is forbidden");
  }

  insertAdjacentHTML() {
    throw new Error("UNSAFE SINK: insertAdjacentHTML is forbidden");
  }
}

/* ---- document --------------------------------------------------------------- */

export class ShimDocument {
  constructor() {
    /** Elements addressable by getElementById / querySelector("#id"). */
    this._roots = [];
    this._listeners = new Map();
    this._registry = new Map();
  }

  _registerRoot(el) {
    if (!this._roots.includes(el)) {
      this._roots.push(el);
    }
  }

  createElement(tag) {
    const el = new ShimElement(tag);
    el.ownerDocument = this;
    this._registry.set(el, tag);
    return el;
  }

  createTextNode(data) {
    return new ShimText(data);
  }

  getElementById(id) {
    for (const root of this._roots) {
      const found = findIn(root, (el) => el.getAttribute("id") === String(id));
      if (found) {
        return found;
      }
    }
    return null;
  }

  querySelector(selector) {
    const all = this.querySelectorAll(selector);
    return all.length > 0 ? all[0] : null;
  }

  /**
   * Search the subtree of each registered root (plus the root itself).
   * @param {string} selector
   * @param {ShimElement} [scope] restrict the search to one element's subtree
   */
  querySelectorAll(selector, scope) {
    const out = [];
    const bases = scope ? [scope] : this._roots;
    for (const base of bases) {
      if (base.nodeType === ELEMENT_NODE && this._matches(base, selector)) {
        out.push(base);
      }
      collectMatches(base, selector, out);
    }
    return out;
  }

  _matches(el, selector) {
    const matcher = compileSelector(selector);
    return matcher ? matcher(el) : false;
  }

  addEventListener(type, handler) {
    if (!this._listeners.has(type)) {
      this._listeners.set(type, []);
    }
    this._listeners.get(type).push(handler);
  }
}

/* ---- selector matching (the small subset actually used) ---------------------- */

function collectMatches(root, selector, out) {
  const matcher = compileSelector(selector);
  if (!matcher) {
    return out;
  }
  const walk = (node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === ELEMENT_NODE) {
        if (matcher(child)) {
          out.push(child);
        }
        walk(child);
      }
    }
  };
  walk(root);
  return out;
}

function findIn(root, predicate) {
  const walk = (node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === ELEMENT_NODE) {
        if (predicate(child)) {
          return child;
        }
        const found = walk(child);
        if (found) {
          return found;
        }
      }
    }
    return null;
  };
  if (root.nodeType === ELEMENT_NODE && predicate(root)) {
    return root;
  }
  return walk(root);
}

/**
 * Supports the compound subset actually used by the application: a tag, any
 * number of `[attr]` / `[attr="value"]` clauses, and `.class` / `#id`.
 * Returns null for anything outside that subset, so the caller finds nothing -
 * which is honest, not permissive.
 */
function compileSelector(selector) {
  const s = String(selector).trim();
  if (!s) {
    return null;
  }
  const rest = s.replace(/^([a-zA-Z][\w-]*)/, (_m, tag) => {
    tagCache = String(tag).toUpperCase();
    return "";
  });
  const tag = tagCache || null;
  tagCache = "";
  if (/[^\[\]="'\w\s.#-]/.test(rest)) {
    return null;
  }
  const attrTests = [];
  const attrRe = /\[([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]/g;
  let m;
  while ((m = attrRe.exec(rest)) !== null) {
    const name = m[1];
    const expected = m[2] !== undefined ? m[2] : m[3] !== undefined ? m[3] : m[4];
    attrTests.push((el) => {
      if (!el.hasAttribute(name)) {
        return false;
      }
      return expected === undefined || el.getAttribute(name) === expected;
    });
  }
  const classes = [];
  const classRe = /\.([\w-]+)/g;
  while ((m = classRe.exec(rest)) !== null) {
    classes.push(m[1]);
  }
  const idMatch = /#([\w-]+)/.exec(rest);
  const id = idMatch ? idMatch[1] : null;

  return (el) => {
    if (tag && el.tagName !== tag) {
      return false;
    }
    for (const test of attrTests) {
      if (!test(el)) {
        return false;
      }
    }
    for (const cls of classes) {
      const own = el.className ? el.className.split(/\s+/) : [];
      if (!own.includes(cls)) {
        return false;
      }
    }
    if (id && el.getAttribute("id") !== id) {
      return false;
    }
    return true;
  };
}

let tagCache = "";

/* ---- environment -------------------------------------------------------------- */

/**
 * Install a fresh DOM/window environment and expose the view root.
 * @returns {{document: ShimDocument, window: object, root: ShimElement,
 *            fireHashChange: (hash: string) => void}}
 */
export function installDom() {
  const doc = new ShimDocument();

  const root = doc.createElement("div");
  root.setAttribute("id", "view-root");
  doc._registerRoot(root);

  const windowListeners = new Map();
  const win = {
    document: doc,
    location: { hash: "#/dashboard" },
    addEventListener(type, handler) {
      if (!windowListeners.has(type)) {
        windowListeners.set(type, []);
      }
      windowListeners.get(type).push(handler);
    },
    removeEventListener(type, handler) {
      const list = windowListeners.get(type) || [];
      const i = list.indexOf(handler);
      if (i >= 0) {
        list.splice(i, 1);
      }
    },
  };

  globalThis.document = doc;
  globalThis.window = win;
  globalThis.Node = ShimNode;
  globalThis.ShimElement = ShimElement;

  return {
    document: doc,
    window: win,
    root,
    /** Drive the real `hashchange` listener registered by app.js `start()`. */
    fireHashChange(hash) {
      win.location.hash = hash;
      for (const handler of (windowListeners.get("hashchange") || []).slice()) {
        handler();
      }
    },
  };
}

export default { installDom, ShimNode, ShimElement, ShimText, ShimDocument };
