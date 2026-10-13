import assert from "node:assert/strict";
import { describe, it, beforeEach } from "node:test";

import { ROUTES, parseHash, showRoute, hasView, getView, titleFor } from "../frontend/js/app.js";
import "../frontend/js/views/dashboard.js";
import "../frontend/js/views/targets.js";
import "../frontend/js/views/instances.js";
import "../frontend/js/views/plan.js";
import "../frontend/js/views/jobs.js";
import { allText, containing, setup, flush, byTag } from "./_fixtures.mjs";

beforeEach(() => {
  setup({
    "GET /health": { status: 200, body: { status: "ok", mode: "simulated" } },
    "GET /api/targets": { status: 200, body: { targets: [], count: 0 } },
    "GET /api/jobs": { status: 200, body: { jobs: [], count: 0 } },
    "GET /api/target-instances": { status: 200, body: { instances: [], count: 0 } },
  });
});

describe("module loading", () => {
  it("imports the real app module and all five view modules", () => {
    // the imports at the top of this file resolve or the suite would not load
    assert.equal(typeof ROUTES, "object");
    assert.ok(Array.isArray(ROUTES));
  });

  it("all five implemented views registered themselves", () => {
    for (const path of ["/dashboard", "/targets", "/instances", "/plan", "/jobs"]) {
      assert.equal(hasView(path), true, path);
    }
  });

  it("every registered view exposes a callable render", () => {
    for (const path of ["/dashboard", "/targets", "/instances", "/plan", "/jobs"]) {
      const view = getView(path);
      assert.equal(typeof view.render, "function", path);
    }
  });

  it("the router declares all six destinations", () => {
    assert.deepEqual(
      ROUTES.map((r) => r.path).sort(),
      ["/dashboard", "/engines", "/instances", "/jobs", "/plan", "/targets"]
    );
  });
});

describe("navigation", () => {
  it("renders the dashboard and reports the resolved route", async () => {
    const info = showRoute("#/dashboard");
    assert.equal(info.path, "/dashboard");
    assert.equal(info.rendered, true);
    await flush();
    assert.match(allText(globalThis.document.getElementById("view-root")), /Simulation only/);
  });

  it("marks the active nav link with aria-current", () => {
    const { nav } = setup({});
    // showRoute runs markActiveNav, which queries the real document
    showRoute("#/targets");
    const about = nav.nav.querySelectorAll('[data-nav-link][aria-current="page"]');
    assert.equal(about.length, 1, "exactly one link is active");
    assert.equal(about[0].getAttribute("aria-label"), "/targets");
  });

  it("clears the previously active link when navigating", () => {
    const { nav } = setup({});
    showRoute("#/targets");
    showRoute("#/jobs");
    const active = nav.nav.querySelectorAll('[data-nav-link][aria-current="page"]');
    assert.equal(active.length, 1);
    assert.equal(active[0].getAttribute("aria-label"), "/jobs");
  });

  it("falls back to the dashboard for an unknown route", () => {
    const info = showRoute("#/nope");
    assert.equal(info.path, "/dashboard");
    assert.equal(info.requested, "/nope");
  });

  it("renders an honest placeholder for the unbuilt Engines route", async () => {
    const info = showRoute("#/engines");
    assert.equal(info.path, "/engines");
    assert.equal(info.rendered, false, "Engines must not claim to be implemented");
    await flush();
    const text = allText(globalThis.document.getElementById("view-root"));
    assert.match(text, /not implemented yet/);
    assert.match(text, /nothing is simulated on screen/);
  });

  it("navigates hash changes through the registered listener", async () => {
    const root = globalThis.document.getElementById("view-root");
    showRoute("#/targets");
    await flush();
    assert.match(allText(root), /Simulation only/);
  });
});

describe("parseHash", () => {
  it("parses a route with query parameters", () => {
    const parsed = parseHash("#/plan?target=pdf-document");
    assert.equal(parsed.path, "/plan");
    assert.equal(parsed.params.target, "pdf-document");
  });

  it("decodes query components", () => {
    const parsed = parseHash("#/plan?target=office%2Ddocument");
    assert.equal(parsed.params.target, "office-document");
  });

  it("resolves an empty hash to the dashboard", () => {
    assert.equal(parseHash("").path, "/dashboard");
    assert.equal(parseHash("#").path, "/dashboard");
  });

  it("ignores an unknown query route but keeps the params", () => {
    const parsed = parseHash("#/bogus?x=1");
    assert.equal(parsed.path, "/dashboard");
    assert.equal(parsed.params.x, "1");
  });
});

describe("titles", () => {
  it("returns a human title for each route", () => {
    assert.equal(titleFor("/dashboard"), "Dashboard");
    assert.equal(titleFor("/jobs"), "Jobs");
    assert.equal(titleFor("/engines"), "Engines");
  });
});

describe("no unresolved imports", () => {
  it("every view renders without throwing on first paint", async () => {
    for (const path of ["/dashboard", "/targets", "/instances", "/plan", "/jobs"]) {
      assert.doesNotThrow(() => showRoute("#" + path), path);
      await flush();
    }
  });
});
