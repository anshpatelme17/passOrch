/*
 * Test fixtures: controllable `fetch` and deterministic API responses.
 *
 * The application talks to the backend only through `frontend/js/api.js`,
 * which calls the global `fetch`. Installing a scripted `fetch` here lets the
 * real client - and therefore every real view - run end to end without a
 * network, a server, or any change to production code.
 *
 * Response bodies are expressed as JS objects and serialized by the fixture,
 * exactly as the backend would.
 */

/** A stand-in for the WHATWG `Response` used by `js/api.js`. */
export class FakeResponse {
  constructor(status, body) {
    this.status = status;
    this._text = body === undefined || body === null ? "" : serialize(body);
  }

  get ok() {
    return this.status >= 200 && this.status < 300;
  }

  async text() {
    return this._text;
  }
}

/**
 * A response spec. Exactly one unambiguous format:
 *
 *   { status: <number>, body: <object|string> }   - an explicit HTTP response
 *   200                                            - a bare status, empty body
 *   204                                            - as above
 *   () => spec                                     - resolved lazily
 *   "raw text"                                     - implied 200, raw body
 *
 * There is deliberately NO "bare body object" form: a job record legitimately
 * carries its own `status` field ("simulated"/"denied"), which must never be
 * mistaken for an HTTP status. See `_regression.test.mjs`.
 */
export const ENVELOPE = "status";

/** True when a route spec is an explicit `{status, body}` envelope. */
export function isEnvelope(spec) {
  return (
    spec !== null &&
    typeof spec === "object" &&
    "status" in spec &&
    typeof spec.status === "number"
  );
}

function serialize(body) {
  if (typeof body === "string") {
    return body;
  }
  return JSON.stringify(body);
}

/**
 * Build a scripted fetch. `routes` is keyed by `"METHOD /path"`; an entry is a
 * response spec (see above). Unscripted requests yield 404
 * `{"detail":"job_not_found"}` so an unexpected call is visible rather than
 * silently succeeding.
 *
 * @param {Record<string, number | {status: number, body?: unknown} | string | (() => number | {status: number, body?: unknown})>} routes
 * @returns {{calls: Array<{url: string, method: string, body: unknown}>, fetch: Function}}
 */
export function installFetch(routes) {
  const table = routes || {};
  const calls = [];

  const fetchImpl = async (url, options) => {
    const opts = options || {};
    const method = String(opts.method || "GET").toUpperCase();
    let parsed = null;
    try {
      parsed = new URL(url, "http://passorch.invalid");
    } catch {
      // keep raw; the client's own path guard should have rejected this already
    }
    const path = parsed ? parsed.pathname : String(url);
    const key = `${method} ${path}`;

    let body;
    if (opts.body !== undefined && opts.body !== null) {
      body = typeof opts.body === "string" ? JSON.parse(opts.body) : opts.body;
    }
    calls.push({ url: String(url), method, body });

    let spec = Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
    if (spec === undefined) {
      // wildcard suffix: "DELETE /api/x/*"
      for (const candidate of Object.keys(table)) {
        if (candidate.endsWith("*")) {
          const prefix = candidate.slice(0, -1);
          if (key.startsWith(prefix)) {
            spec = table[candidate];
            break;
          }
        }
      }
    }
    if (typeof spec === "function") {
      spec = spec();
    }
    if (spec === undefined) {
      return new FakeResponse(404, { detail: "job_not_found" });
    }
    if (typeof spec === "number") {
      return new FakeResponse(spec, undefined);
    }
    if (typeof spec === "string") {
      return new FakeResponse(200, spec);
    }
    if (isEnvelope(spec)) {
      return new FakeResponse(spec.status, spec.body);
    }
    throw new Error(
      `ambiguous response spec for ${key}: expected {status, body} with a numeric status`
    );
  };

  globalThis.fetch = fetchImpl;
  return { calls, fetch: fetchImpl };
}

/** Force `fetch` to fail at the network level. */
export function installFailingFetch(message = "network down") {
  globalThis.fetch = async () => {
    throw new Error(message);
  };
}

/** Remove every global the harness installs, so state cannot leak. */
export function resetGlobals() {
  delete globalThis.fetch;
  delete globalThis.document;
  delete globalThis.window;
  delete globalThis.Node;
  delete globalThis.ShimElement;
}

/* ---- canned backend payloads (shapes verified against app/api/schemas.py) ---- */

export const CATALOG = {
  targets: [
    {
      target: "office-document",
      description: "Office document encryption audit",
      formats: ["office2007", "office2010", "office2013"],
      enabled: true,
      usable: true,
    },
    {
      target: "pdf-document",
      description: "PDF document encryption audit",
      formats: ["pdf"],
      enabled: true,
      usable: true,
    },
    {
      target: "zip-archive",
      description: "Archive encryption audit",
      formats: ["pkzip", "zip"],
      enabled: true,
      usable: false,
    },
  ],
  count: 3,
};

export const EMPTY_CATALOG = { targets: [], count: 0 };

export const JOBS_LIST = {
  jobs: [
    {
      job_id: "11111111-1111-4111-8111-111111111111",
      status: "simulated",
      created_at: 1700000001,
      target: "pdf-document",
    },
    {
      job_id: "22222222-2222-4222-8222-222222222222",
      status: "denied",
      created_at: 1700000002,
      target: "zip-archive",
    },
  ],
  count: 2,
};

export const EMPTY_JOBS = { jobs: [], count: 0 };

export const INSTANCE_LIST = {
  instances: [
    {
      id: "33333333-3333-4333-8333-333333333333",
      name: "Q3 archive",
      target_type: "zip-archive",
      description: "Archive target",
      created_at: 1700000000,
    },
    {
      id: "44444444-4444-4444-8444-444444444444",
      name: "Contract PDF",
      target_type: "pdf-document",
      description: "",
      created_at: 1700000005,
    },
  ],
  count: 2,
};

export const EMPTY_INSTANCES = { instances: [], count: 0 };

/** A job that was authorized and simulated. */
export function simulatedJob(instanceId, jobId) {
  return {
    job_id: jobId || "55555555-5555-4555-8555-555555555555",
    status: "simulated",
    created_at: 1700000010,
    target: "pdf-document",
    failure_reason: "",
    instance_id: instanceId || "",
    instance_target_type: instanceId ? "pdf-document" : "",
    preview: {
      target: "pdf-document",
      selection: {
        status: "selected",
        reason: "verified_capability",
        selected_engine: "john",
        candidates: ["john"],
      },
      request: {
        target: "pdf-document",
        engine: "mock_john",
        mode: "simulated",
        selection_status: "selected",
        capability_status: "verified",
        selected_engine: "john",
        matched_formats: ["pdf"],
        reference: "r",
      },
      authorization: {
        authorized: true,
        status: "authorized",
        reason: "authorized_simulation",
        detail: "",
        problems: [],
        adapter: "mock_john",
        mode: "simulated",
      },
      plan: {
        adapter: "mock_john",
        mode: "simulated",
        simulated: true,
        steps: ["load_mock_engine", "simulate_compatibility_run"],
        target: "pdf-document",
        selected_engine: "john",
        capability_status: "verified",
      },
      simulation: {
        status: "simulated",
        adapter: "mock_john",
        engine: "john",
        target: "pdf-document",
        mode: "simulated",
        steps: ["load_mock_engine", "simulate_compatibility_run"],
        message: "simulation completed",
        simulation_id: "abcdef0123456789",
        simulated: true,
      },
    },
  };
}

/** A job the policy denied - still HTTP 200, still recorded. */
export function deniedJob(reason = "capability_not_verified") {
  return {
    job_id: "66666666-6666-4666-8666-666666666666",
    status: "denied",
    created_at: 1700000020,
    target: "pdf-document",
    failure_reason: reason,
    instance_id: "",
    instance_target_type: "",
    preview: {
      target: "pdf-document",
      selection: {
        status: "no_verified_capability",
        reason: "capability_missing",
        selected_engine: "",
        candidates: [],
      },
      request: {
        target: "pdf-document",
        engine: "",
        mode: "simulated",
        selection_status: "no_verified_capability",
        capability_status: "",
        selected_engine: "",
        matched_formats: [],
        reference: "",
      },
      authorization: {
        authorized: false,
        status: "denied",
        reason: reason,
        detail: "capability evidence is not VERIFIED",
        problems: [],
        adapter: "",
        mode: "simulated",
      },
      plan: null,
      simulation: null,
    },
  };
}

/** A denied plan preview: HTTP 200 with authorization.authorized === false. */
export function deniedPlan(reason = "capability_not_verified") {
  return {
    target: "pdf-document",
    selection: {
      status: "no_verified_capability",
      reason: "capability_missing",
      selected_engine: "",
      candidates: [],
    },
    request: {
      target: "pdf-document",
      engine: "",
      mode: "simulated",
      selection_status: "no_verified_capability",
      capability_status: "",
      selected_engine: "",
      matched_formats: [],
      reference: "r",
    },
    authorization: {
      authorized: false,
      status: "denied",
      reason: reason,
      detail: "capability evidence is not VERIFIED",
      problems: [],
      adapter: "",
      mode: "simulated",
    },
    plan: null,
    simulation: null,
  };
}

/* ---- tree helpers ------------------------------------------------------------- */

/**
 * Flatten a shim tree into a list of `{tag, text, class, attrs}` records.
 * Cheap, stable, and avoids brittle whole-tree string comparisons.
 */
export function flatten(node, out) {
  out = out || [];
  for (const child of node.childNodes) {
    if (child.nodeType === 1) {
      out.push({
        tag: child.tagName.toLowerCase(),
        text: child.textContent,
        class: child.className,
        attrs: Object.fromEntries(child.attrs),
        node: child,
      });
      flatten(child, out);
    }
  }
  return out;
}

/** Every text value in the subtree, in document order. */
export function allText(node) {
  return flatten(node)
    .map((n) => n.text)
    .filter(Boolean)
    .join("\n");
}

/**
 * The concatenated `textContent` of a subtree. Used for substring assertions
 * that do not care about element boundaries.
 */
export function rootText(node) {
  return node.textContent;
}

/** Find elements by tag name within a subtree. */
export function byTag(node, tag) {
  return flatten(node).filter((n) => n.tag === tag);
}

/** Find a subtree containing `needle` in its text. */
export function containing(node, needle) {
  const walk = (n) => {
    if (n.nodeType === 3) {
      return null;
    }
    if (n.textContent.includes(needle)) {
      return n;
    }
    for (const c of n.childNodes) {
      const found = walk(c);
      if (found) {
        return found;
      }
    }
    return null;
  };
  return walk(node);
}

/** The <option> values of a <select>, in order. */
export function selectValues(select) {
  return byTag(select, "option").map((o) => o.attrs.value);
}

/** Simulate a real `input` event on an element. */
export function type(el, value) {
  el.value = value;
  el.dispatchEvent({ type: "input", target: el });
}

/** Simulate a real `click` on an element. */
export function click(el) {
  el.dispatchEvent({ type: "click", target: el });
}

/**
 * Let queued async work drain.
 *
 * Uses macrotask ticks rather than microtasks only: a view may await one
 * request *before* issuing the next (e.g. `instances.js` loads the target
 * catalog before rendering the list), so a fixed number of microtask drains is
 * not enough to observe the settled state.
 */
export function flush(times = 8) {
  let p = Promise.resolve();
  for (let i = 0; i < times; i += 1) {
    p = p.then(() => new Promise((resolve) => setTimeout(resolve, 0)));
  }
  return p;
}

/**
 * Install a DOM + fetch environment in one call and return a typed handle.
 * Intended for use inside `beforeEach`.
 */
export function setup(routes) {
  const env = installDom();
  const net = installFetch(routes);
  return {
    ...env,
    ...net,
    nav: installNav(env.document),
    /** Navigate through the real hashchange path and let views settle. */
    async go(hash, flushes = 6) {
      env.fireHashChange(hash);
      await flush(flushes);
    },
  };
}

/**
 * Build the navigation bar exactly as `frontend/index.html` declares it, so
 * `markActiveNav` in app.js has real elements to mark.
 */
export function installNav(doc) {
  const routes = ["/dashboard", "/engines", "/targets", "/instances", "/plan", "/jobs"];
  const nav = doc.createElement("nav");
  nav.setAttribute("class", "app-nav");
  const links = [];
  for (const route of routes) {
    const a = doc.createElement("a");
    a.setAttribute("href", `#${route}`);
    a.setAttribute("data-nav-link", "");
    a.setAttribute("aria-label", route);
    a.textContent = route.replace("/", "");
    nav.appendChild(a);
    links.push(a);
  }
  doc._registerRoot(nav);
  return { nav, links, byRoute: (r) => links[routes.indexOf(r)] };
}

function installDom() {
  // imported lazily to avoid a cycle in the module graph
  return _dom.installDom();
}

import * as _dom from "./_dom-shim.mjs";
