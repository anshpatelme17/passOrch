import assert from "node:assert/strict";
import { describe, it, beforeEach } from "node:test";

import { ROUTES, parseHash, showRoute, hasView, getView, titleFor } from "../frontend/js/app.js";
import "../frontend/js/views/dashboard.js";
import "../frontend/js/views/engines.js";
import "../frontend/js/views/targets.js";
import "../frontend/js/views/instances.js";
import "../frontend/js/views/plan.js";
import "../frontend/js/views/jobs.js";
import { allText, click, containing, rootText, setup, flush, byTag } from "./_fixtures.mjs";

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

  it("renders the Engines view", async () => {
    const info = showRoute("#/engines");
    assert.equal(info.path, "/engines");
    assert.equal(info.rendered, true, "Engines view must be implemented");
    await flush();
    const text = allText(globalThis.document.getElementById("view-root"));
    assert.match(text, /Simulation only/);
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

  it("keeps a malformed value instead of throwing on a bad escape", () => {
    // `%zz` is not a valid hex escape: decodeURIComponent throws URIError.
    const parsed = parseHash("#/plan?target=%zz");
    assert.equal(parsed.path, "/plan");
    assert.equal(parsed.params.target, "%zz");
  });

  it("keeps a malformed key instead of throwing on a bad escape", () => {
    const parsed = parseHash("#/plan?%zz=1");
    assert.equal(parsed.path, "/plan");
    assert.equal(parsed.params["%zz"], "1");
  });

  it("survives a truncated escape at the end of the query", () => {
    const parsed = parseHash("#/jobs?x=abc%");
    assert.equal(parsed.path, "/jobs");
    assert.equal(parsed.params.x, "abc%");
  });

  it("renders a malformed hash without taking the router down", () => {
    // The router must still resolve the route and paint a view.
    let info = null;
    assert.doesNotThrow(() => {
      info = showRoute("#/plan?target=%zz");
    });
    assert.equal(info.path, "/plan");
    assert.match(rootText(root()), /Simulation only/);
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

/* ---- engines discovery refresh (M1.3) ------------------------------------ */

const VERIFIED_JOHN = {
  engine: "john",
  discovered: true,
  binary_found: true,
  version: "1.8.0",
  operations: ["--list=formats"],
  snapshot_error: null,
  capability_status: "verified",
  formats: ["pdf", "ntlm"],
  targets: ["pdf-document"],
  source_operation: "--list=formats",
  reason: null,
  parser: "john-format-parser",
  real_engine_compatible: true,
};
const UNKNOWN_HASHCAT = {
  engine: "hashcat",
  discovered: false,
  binary_found: false,
  version: null,
  operations: [],
  snapshot_error: null,
  capability_status: "unknown",
  formats: [],
  targets: [],
  source_operation: null,
  reason: "binary_not_found",
  parser: null,
  real_engine_compatible: true,
};
const BOTH_ENGINES = { engines: [VERIFIED_JOHN, UNKNOWN_HASHCAT] };

function report(engine, status, reason, truncated) {
  return {
    engine,
    discovered: status === "verified",
    capability_status: status,
    reason: reason || "",
    truncated: truncated === true,
  };
}

function start(routes) {
  const env = setup({
    "GET /api/engines": { status: 200, body: BOTH_ENGINES },
    ...(routes || {}),
  });
  return env;
}
function root() {
  return globalThis.document.getElementById("view-root");
}
function btn(label) {
  return root().querySelectorAll("button").find((b) => new RegExp(label).test(b.textContent));
}

describe("engines - refresh is user-initiated only", () => {
  it("page load issues exactly one GET and never a POST", async () => {
    const env = start({});
    showRoute("#/engines");
    await flush();
    const posts = env.calls.filter((c) => c.method === "POST");
    assert.deepEqual(posts, []);
    assert.equal(env.calls.filter((c) => c.method === "GET").length, 1);
  });

  it("navigating away and back never refreshes automatically", async () => {
    const env = start({});
    showRoute("#/engines");
    await flush();
    showRoute("#/dashboard");
    await flush();
    showRoute("#/engines");
    await flush();
    assert.deepEqual(
      env.calls.filter((c) => c.method === "POST"),
      []
    );
  });
});

describe("engines - refresh interaction", () => {
  it("the button exists and the metadata-only wording is shown", async () => {
    start({});
    showRoute("#/engines");
    await flush();
    assert.ok(btn("Refresh engines"), "refresh button must be rendered");
    const text = rootText(root());
    assert.match(text, /Discovery checks installed engine metadata/);
    assert.match(text, /does not perform password auditing or cracking/i);
  });

  it("clicking posts to the exact endpoint with no body, then re-fetches the list", async () => {
    const env = start({
      "POST /api/engines/refresh": {
        status: 200,
        body: { engines: [report("john", "verified")] },
      },
    });
    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    await flush();
    const posts = env.calls.filter((c) => c.method === "POST");
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, "/api/engines/refresh");
    assert.equal(posts[0].body, undefined, "the refresh POST must carry no body");
    const gets = env.calls.filter((c) => c.method === "GET");
    assert.equal(gets.length, 2, "the engine list is fetched again after the refresh");
    assert.match(rootText(root()), /Metadata discovery finished/);
  });

  it("disables the button and shows a loading state while in flight", async () => {
    start({
      "POST /api/engines/refresh": {
        status: 200,
        body: { engines: [report("john", "verified")] },
      },
    });
    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    // dispatchEvent is synchronous: the busy state renders before the first
    // request resolves, so this asserts the true in-flight UI.
    assert.equal(btn("Refresh engines").hasAttribute("disabled"), true);
    assert.match(rootText(root()), /Discovering engine metadata/);
    await flush();
    assert.equal(btn("Refresh engines").hasAttribute("disabled"), false);
  });

  it("a second click while in flight never queues a second refresh", async () => {
    const env = start({
      "POST /api/engines/refresh": {
        status: 200,
        body: { engines: [report("john", "verified")] },
      },
    });
    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    click(btn("Refresh engines"));
    await flush();
    assert.equal(env.calls.filter((c) => c.method === "POST").length, 1);
  });

  it("a refresh left pending across navigation does not latch the button", async () => {
    const env = start({
      "POST /api/engines/refresh": {
        status: 200,
        body: { engines: [report("john", "verified")] },
      },
    });
    const fixtureFetch = env.fetch;
    let posts = 0;
    globalThis.fetch = (url, opts) => {
      const method = String((opts && opts.method) || "GET").toUpperCase();
      if (method === "POST" && String(url).startsWith("/api/engines/refresh")) {
        posts += 1;
        if (posts === 1) {
          return new Promise(() => {}); // hangs: its `finally` never runs
        }
      }
      return fixtureFetch(url, opts);
    };

    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    await flush();
    assert.equal(posts, 1, "the first refresh must be issued");

    // Leave while that request is still in flight, then come back.
    showRoute("#/dashboard");
    await flush();
    showRoute("#/engines");
    await flush();

    click(btn("Refresh engines"));
    await flush();
    assert.equal(posts, 2, "re-entry must reset the latched refresh guard");
  });

  it("distinguishes verified from unknown and names the unverified engine", async () => {
    start({
      "POST /api/engines/refresh": {
        status: 200,
        body: {
          engines: [report("john", "verified"), report("hashcat", "unknown", "binary_not_found")],
        },
      },
    });
    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    await flush();
    const text = rootText(root());
    // Partial success: one of two verified - stated as such, not as failure.
    assert.match(text, /1 of 2 engine\(s\) hold verified capability evidence/);
    assert.match(text, /hashcat remains unverified \(unknown: binary_not_found\)/);
    const badges = byTag(root(), "span")
      .filter((n) => /^badge badge--/.test(n.class))
      .map((n) => n.class);
    assert.ok(badges.includes("badge badge--ok"), "verified badge shown");
    assert.ok(badges.includes("badge badge--warn"), "unknown badge shown");
  });

  it("all verified reads as full success", async () => {
    start({
      "POST /api/engines/refresh": { status: 200, body: { engines: [report("john", "verified")] } },
    });
    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    await flush();
    assert.match(rootText(root()), /all 1 engine\(s\) hold verified capability evidence/);
  });
});

describe("engines - refresh failure handling", () => {
  const cases = [
    [401, { error: "unauthorized", detail: "a valid bearer token is required" }, /operator token/i],
    [
      409,
      { error: "refresh_in_progress", detail: "another discovery refresh is already running" },
      /Another discovery refresh is already running/,
    ],
    [422, { error: "invalid_request", detail: "invalid request" }, /rejected as invalid/],
    [
      429,
      { error: "rate_limited", detail: "too many requests" },
      /Too many refresh requests/,
    ],
    [
      503,
      { error: "discovery_refresh_failed", detail: "engine discovery refresh could not be completed" },
      /unavailable on the server/,
    ],
  ];
  for (const [status, detail, hint] of cases) {
    it(`surfaces ${status} with a safe hint and no internals`, async () => {
      const env = start({
        "POST /api/engines/refresh": { status, body: { detail } },
      });
      showRoute("#/engines");
      await flush();
      click(btn("Refresh engines"));
      await flush();
      const text = rootText(root());
      assert.match(text, hint);
      // The failure never fabricates engine data or claims an audit ran.
      assert.doesNotMatch(text, /cracked|password found/i);
      // A failed refresh does not re-fetch the list.
      assert.equal(env.calls.filter((c) => c.method === "GET").length, 1);
      // The button is usable again.
      assert.equal(btn("Refresh engines").hasAttribute("disabled"), false);
    });
  }

  it("handles a network failure without losing the existing list", async () => {
    start({});
    showRoute("#/engines");
    await flush();
    const goodFetch = globalThis.fetch;
    globalThis.fetch = (url, opts) => {
      if (opts && String(opts.method).toUpperCase() === "POST") {
        return Promise.reject(new Error("network down"));
      }
      return goodFetch(url, opts);
    };
    click(btn("Refresh engines"));
    await flush();
    assert.match(rootText(root()), /Cannot reach the PassOrch API/);
    assert.match(rootText(root()), /john/, "the previously loaded list stays visible");
  });

  it("communicates refresh-success followed by list failure", async () => {
    let gets = 0;
    start({
      "GET /api/engines": () => {
        gets += 1;
        return gets === 1
          ? { status: 200, body: BOTH_ENGINES }
          : { status: 503, body: { detail: { error: "not_ready", detail: "readiness probe failed" } } };
      },
      "POST /api/engines/refresh": {
        status: 200,
        body: { engines: [report("john", "verified")] },
      },
    });
    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    await flush();
    const text = rootText(root());
    assert.match(text, /Discovery completed, but the engine list could not be updated/);
    assert.match(text, /The refresh itself succeeded; only the list below could not be reloaded/);
    assert.ok(btn("Retry loading the engine list"), "an explicit retry is offered");
  });

  it("the retry after a list failure really re-fetches the list", async () => {
    let gets = 0;
    start({
      "GET /api/engines": () => {
        gets += 1;
        return gets === 1
          ? { status: 200, body: BOTH_ENGINES }
          : gets === 2
            ? { status: 503, body: { detail: { error: "not_ready", detail: "readiness probe failed" } } }
            : { status: 200, body: BOTH_ENGINES };
      },
      "POST /api/engines/refresh": {
        status: 200,
        body: { engines: [report("john", "verified")] },
      },
    });
    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    await flush();
    click(btn("Retry loading the engine list"));
    await flush();
    assert.equal(gets, 3);
    assert.doesNotMatch(rootText(root()), /could not be updated/);
  });
});

describe("engines - truncated discovery evidence", () => {
  it("warns on a truncated report while preserving the backend status", async () => {
    start({
      "POST /api/engines/refresh": {
        status: 200,
        body: { engines: [report("john", "unknown", "truncated_probe", true)] },
      },
    });
    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    await flush();
    const text = rootText(root());
    assert.match(text, /Truncated discovery output for john/);
    assert.match(text, /captured metadata was incomplete/);
    assert.match(text, /must not be interpreted as verified capability evidence/);
    // The backend's own status is named as unchanged - truncated UNKNOWN
    // stays UNKNOWN (the structured reason stays visible and safe).
    assert.match(text, /status \(unknown\) is unchanged/);
    assert.match(text, /john remains unverified \(unknown: truncated_probe\)/);
    // It is a caveat on the evidence, not a failure of the refresh.
    assert.match(text, /Metadata discovery finished: 0 of 1 engine\(s\)/);
    assert.doesNotMatch(text, /The refresh did not run/);
    assert.doesNotMatch(text, /could not be updated/);
  });

  it("shows no truncation warning for complete reports", async () => {
    start({
      "POST /api/engines/refresh": {
        status: 200,
        body: { engines: [report("john", "verified")] },
      },
    });
    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    await flush();
    const text = rootText(root());
    assert.doesNotMatch(text, /truncated/i);
    assert.match(text, /all 1 engine\(s\) hold verified capability evidence/);
  });

  it("keeps partial success intact when one engine is truncated", async () => {
    start({
      "POST /api/engines/refresh": {
        status: 200,
        body: {
          engines: [
            report("john", "verified"),
            report("hashcat", "unknown", "truncated_output", true),
          ],
        },
      },
    });
    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    await flush();
    const text = rootText(root());
    // Success-with-detail framing survives: one of two verified, plus caveats.
    assert.match(text, /Metadata discovery finished: 1 of 2 engine\(s\) hold verified/);
    assert.match(text, /Truncated discovery output for hashcat/);
    assert.match(text, /hashcat remains unverified \(unknown: truncated_output\)/);
    assert.doesNotMatch(text, /The refresh did not run/);
  });

  it("never displays raw discovery output, even if a payload carried it", async () => {
    start({
      "POST /api/engines/refresh": {
        status: 200,
        body: {
          engines: [
            {
              ...report("john", "unknown", "truncated_probe", true),
              stdout: "RAW-STDOUT john --list=formats SECRET",
              stderr: "RAW-STDERR traceback",
            },
          ],
        },
      },
    });
    showRoute("#/engines");
    await flush();
    click(btn("Refresh engines"));
    await flush();
    const text = rootText(root());
    assert.match(text, /Truncated discovery output for john/);
    assert.doesNotMatch(text, /RAW-STDOUT/);
    assert.doesNotMatch(text, /RAW-STDERR/);
    assert.doesNotMatch(text, /stdout|stderr/i);
  });
});
