/*
 * Safe DOM helpers.
 *
 * Every write goes through `text` / `textNode`; there is no code path that
 * assigns `innerHTML`, `outerHTML` or `insertAdjacentHTML`, and no `eval`,
 * `new Function` or `document.write`. User-controlled strings therefore can
 * only ever become text nodes and cannot be parsed as markup.
 *
 * These helpers also give one place to implement the recurring UI states
 * (pending / empty / error) so views stay small and consistent.
 */

export const NAMESPACE = "http://www.w3.org/1999/xhtml";

/** Create an element with a `textContent` value. Never parses markup. */
export function h(tag, options) {
  const opts = options || {};
  const el = document.createElement(tag);
  if (opts.class) {
    el.className = opts.class;
  }
  if (opts.text !== undefined && opts.text !== null) {
    el.textContent = String(opts.text);
  }
  if (Array.isArray(opts.attrs)) {
    for (const pair of opts.attrs) {
      if (!Array.isArray(pair) || pair.length !== 2) {
        continue;
      }
      el.setAttribute(String(pair[0]), String(pair[1]));
    }
  }
  if (Array.isArray(opts.children)) {
    for (const child of opts.children) {
      if (child instanceof Node) {
        el.appendChild(child);
      } else if (child !== null && child !== undefined) {
        el.appendChild(text(child));
      }
    }
  }
  if (typeof opts.onClick === "function") {
    el.addEventListener("click", opts.onClick);
  }
  if (typeof opts.onChange === "function") {
    el.addEventListener("change", opts.onChange);
  }
  if (typeof opts.onInput === "function") {
    el.addEventListener("input", opts.onInput);
  }
  return el;
}

/** Wrap an untrusted value in a text node. */
export function text(value) {
  return document.createTextNode(value === undefined || value === null ? "" : String(value));
}

/** Remove every child of `node`. */
export function clear(node) {
  if (node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }
  return node;
}

/** Replace `node`'s children with the supplied elements. */
export function render(node, children) {
  clear(node);
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child instanceof Node) {
      node.appendChild(child);
    } else if (child !== null && child !== undefined) {
      node.appendChild(text(child));
    }
  }
  return node;
}

/* ---- elements ----------------------------------------------------------- */

export function table(headers, rows) {
  const thead = h("thead", {});
  const headRow = h("tr", {});
  for (const label of headers) {
    headRow.appendChild(h("th", { text: label }));
  }
  thead.appendChild(headRow);

  const tbody = h("tbody", {});
  for (const row of rows) {
    const tr = h("tr", {});
    for (const cell of row) {
      if (cell instanceof Node) {
        tr.appendChild(h("td", { children: [cell] }));
      } else {
        tr.appendChild(h("td", { text: cell === undefined || cell === null ? "" : cell }));
      }
    }
    tbody.appendChild(tr);
  }

  return h("div", { class: "table-wrap", children: [h("table", { class: "data", children: [thead, tbody] })] });
}

export function badge(value, tone) {
  const cls = tone ? `badge badge--${tone}` : "badge badge--muted";
  return h("span", { class: cls, text: value });
}

export function button(label, onClick, className) {
  return h("button", {
    type: "button",
    class: className || "btn",
    text: label,
    onClick,
  });
}

export function codeBlock(value) {
  return h("code", { text: value });
}

/**
 * A definition-style key/value block. Values may be a string, number or Node;
 * strings are written as text so untrusted input can never become markup.
 */
export function kv(rows) {
  const wrap = h("div", { class: "kv" });
  if (!Array.isArray(rows)) {
    return wrap;
  }
  for (const row of rows) {
    const key = h("dt", { class: "kv__key", text: row[0] ?? "" });
    let value;
    if (row[1] instanceof Node) {
      value = h("dd", { class: "kv__value", children: [row[1]] });
    } else {
      value = h("dd", {
        class: "kv__value",
        text: row[1] === undefined || row[1] === null ? "" : row[1],
      });
    }
    const line = h("div", { class: "kv__row", children: [key, value] });
    wrap.appendChild(line);
  }
  return wrap;
}

export function panel(title, children, hint) {
  const head = h("div", { class: "panel__head" });
  head.appendChild(h("h2", { class: "panel__title", text: title }));
  if (hint) {
    head.appendChild(h("p", { class: "panel__hint", text: hint }));
  }
  return h("section", { class: "panel", children: [head, ...children] });
}

/* ---- states ------------------------------------------------------------- */

export function pending(message) {
  return h("div", {
    class: "state state--pending",
    attrs: [["role", "status"]],
    children: [h("span", { class: "spinner", attrs: [["aria-hidden", "true"]] }), h("span", { text: message })],
  });
}

export function empty(message, hint) {
  const kids = [h("p", { class: "state__title", text: message })];
  if (hint) {
    kids.push(h("p", { class: "state__hint", text: hint }));
  }
  return h("div", { class: "state state--empty", children: kids });
}

/**
 * Render an API failure. Accepts the `error` object produced by js/api.js or a
 * plain Error, and always degrades to a generic sentence so no internal detail
 * is required for the user to act.
 */
export function errorState(error) {
  const err =
    error && typeof error === "object" && "kind" in error
      ? error
      : { kind: "error", message: String((error && error.message) || error || "unknown error") };

  const title =
    err.kind === "transport"
      ? "Cannot reach the PassOrch API"
      : err.kind === "invalid_request"
        ? "Invalid request"
        : err.kind === "http"
          ? "The API refused this request"
          : "Something went wrong";

  const kids = [h("p", { class: "state__title", text: title })];
  kids.push(h("p", { class: "state__hint", text: err.message || "" }));
  if (err.kind === "transport") {
    kids.push(
      h("p", {
        class: "state__hint",
        text: "Check that the server is running, then retry.",
      })
    );
  }
  return h("div", {
    class: "state state--error",
    attrs: [["role", "alert"]],
    children: kids,
  });
}

/**
 * Honest placeholder for a route that has no data yet. Used until each screen
 * is implemented so navigation works without fabricating anything.
 */
export function placeholder(title, note) {
  return h("div", {
    class: "state",
    children: [
      h("p", { class: "state__title", text: title }),
      h("p", { class: "state__hint", text: note }),
    ],
  });
}
