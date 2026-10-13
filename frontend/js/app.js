/*
 * PassOrch application shell: hash router + view registry.
 *
 * Phase 4A implements the shell only. Registered views render real structure
 * against the real API where a screen is complete; unregistered routes show
 * the honest placeholder from js/components.js. No view fabricates data or
 * reports a success it did not receive.
 *
 * SECURITY BOUNDARY
 * No innerHTML/outerHTML/insertAdjacentHTML/document.write/eval/new Function.
 * All rendering goes through the textContent-based helpers in js/dom.js. The
 * router only ever navigates to routes in ROUTES, so the hash cannot be used to
 * load arbitrary paths.
 */

import { api } from "./api.js";
import { SimulationNotice } from "./components.js";
import { clear, errorState, placeholder, render } from "./dom.js";

const VIEW_ROOT_ID = "view-root";

/** The six navigation destinations. Keys are the hash routes. */
export const ROUTES = Object.freeze([
  { path: "/dashboard", title: "Dashboard", view: null },
  { path: "/engines", title: "Engines", view: null },
  { path: "/targets", title: "Targets", view: null },
  { path: "/instances", title: "Instances", view: null },
  { path: "/plan", title: "Plan Preview", view: null },
  { path: "/jobs", title: "Jobs", view: null },
]);

const ROUTE_PATHS = new Set(ROUTES.map((r) => r.path));

/* ---- view registration -------------------------------------------------- */

const views = new Map();

/**
 * Register a view for one of the six routes.
 * @param {string} path
 * @param {{ title: string, render: (root: HTMLElement, params: object) => void }} view
 */
export function registerView(path, view) {
  if (!ROUTE_PATHS.has(path)) {
    throw new Error(`unknown route: ${path}`);
  }
  if (view === null || typeof view.render !== "function") {
    throw new Error(`view for ${path} must expose render(root, params)`);
  }
  views.set(path, view);
  return view;
}

/** True when a route has a view registered. Used by the tests. */
export function hasView(path) {
  return views.has(path);
}

/** The registered view for a route, or null. */
export function getView(path) {
  return views.get(path) || null;
}

/* ---- navigation --------------------------------------------------------- */

/** Parse `location.hash` into `{ path, params }`; unknown -> the dashboard. */
export function parseHash(hash) {
  const raw = typeof hash === "string" && hash.startsWith("#") ? hash.slice(1) : hash || "";
  const [pathPart, queryPart] = raw.split("?");
  const path = pathPart || "/dashboard";
  const resolved = ROUTE_PATHS.has(path) ? path : "/dashboard";

  const params = {};
  if (queryPart) {
    for (const pair of queryPart.split("&")) {
      const [key, value] = pair.split("=");
      if (key) {
        params[decodeURIComponent(key)] = decodeURIComponent(value || "");
      }
    }
  }
  return { path: resolved, params, requested: path };
}

function markActiveNav(path) {
  const links = document.querySelectorAll("[data-nav-link]");
  for (const link of links) {
    const href = typeof link.getAttribute === "function" ? link.getAttribute("href") : "";
    if (href === `#${path}`) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  }
}

function viewRoot() {
  return document.getElementById(VIEW_ROOT_ID);
}

/** Render one route. Exported so tests can drive navigation directly. */
export function showRoute(hash) {
  const root = viewRoot();
  if (!root) {
    return null;
  }
  const { path, params, requested } = parseHash(hash);
  markActiveNav(path);

  const view = getView(path);
  if (!view) {
    render(root, [
      SimulationNotice(),
      placeholder(
        `${titleFor(path)} is not implemented yet`,
        "This screen has no data yet. It will show live results from the PassOrch API once implemented - nothing is simulated on screen."
      ),
    ]);
    return { path, params, requested, rendered: false };
  }

  clear(root);
  try {
    view.render(root, params);
  } catch (err) {
    render(root, [SimulationNotice(), errorState(err)]);
  }
  return { path, params, requested, rendered: true };
}

export function titleFor(path) {
  const found = ROUTES.find((r) => r.path === path);
  return found ? found.title : "Dashboard";
}

/* ---- shell bootstrap ---------------------------------------------------- */

async function renderBannerHealth() {
  // /health is the only call the shell makes on its own; a failure here must
  // never block navigation, so the banner text is static regardless.
  try {
    const res = await api.get("/health");
    if (res.ok && res.data && res.data.mode) {
      const note = document.querySelector("[data-simulation-notice]");
      if (note) {
        note.setAttribute("data-mode", String(res.data.mode));
      }
    }
  } catch {
    /* banner stays textual */
  }
}

/**
 * Start the shell.
 * @param {{ container?: HTMLElement, afterRoute?: (info: object) => void }} [options]
 */
export function start(options) {
  const opts = options || {};
  const root = opts.container || viewRoot();
  if (!root) {
    return null;
  }

  const onHashChange = () => {
    const info = showRoute(window.location.hash);
    if (opts.afterRoute) {
      opts.afterRoute(info);
    }
  };

  window.addEventListener("hashchange", onHashChange);
  renderBannerHealth();
  onHashChange();
  return { showRoute, registerView, stop: () => window.removeEventListener("hashchange", onHashChange) };
}

export default Object.freeze({ ROUTES, start, showRoute, parseHash, registerView, hasView, getView });
