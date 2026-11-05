import assert from "node:assert/strict";
import { describe, it, beforeEach } from "node:test";

import { showRoute } from "../app/frontend/js/app.js";
import "../app/frontend/js/views/dashboard.js";
import { formatTimestamp } from "../app/frontend/js/dom.js";
import { FakeResponse, assertLoneShellBanner, containing, flush, setup } from "./_fixtures.mjs";

const HEALTH = { status: "ok", mode: "simulated" };
const ENGINES = {
  engines: [
    {
      engine: "john",
      discovered: true,
      binary_found: true,
      version: null,
      operations: [],
      snapshot_error: null,
      capability_status: "verified",
      formats: ["pdf"],
      targets: [],
      source_operation: null,
      reason: null,
      parser: null,
      real_engine_compatible: true,
    },
  ],
};
const JOBS = {
  jobs: [
    { job_id: "id-1", status: "simulated", created_at: 1700000001, target: "pdf-document" },
    { job_id: "id-2", status: "denied", created_at: 1700000002, target: "zip-archive" },
  ],
  count: 2,
};
const INSTANCES = {
  instances: [
    {
      id: "33333333-3333-4333-8333-333333333333",
      name: "Contract PDF",
      target_type: "pdf-document",
      description: "",
      created_at: 1700000005,
    },
  ],
  count: 1,
};

let env;

/** Healthy routes, with per-test overrides keyed by "METHOD /path". */
const STATS = { total: 2, simulated: 1, denied: 1, failed: 0, by_status: { simulated: 1, denied: 1 }, by_target: { "pdf-document": 1, "zip-archive": 1 } };

function start(overrides) {
  env = setup({
    "GET /health": { status: 200, body: HEALTH },
    "GET /api/engines": { status: 200, body: ENGINES },
    "GET /api/jobs": { status: 200, body: JOBS },
    "GET /api/jobs/stats": { status: 200, body: STATS },
    "GET /api/target-instances": { status: 200, body: INSTANCES },
    ...overrides,
  });
  return env;
}

function root() {
  return globalThis.document.getElementById("view-root");
}

/** Replace fetch with a stub that maps path suffixes to canned responses. */
function stub(map) {
  globalThis.fetch = async (url) => {
    const target = String(url);
    for (const [suffix, spec] of Object.entries(map)) {
      if (target.endsWith(suffix)) {
        return typeof spec === "function" ? spec() : new FakeResponse(spec.status, spec.body);
      }
    }
    return new FakeResponse(404, { detail: "job_not_found" });
  };
}

beforeEach(() => {
  start();
});

describe("dashboard - happy path", () => {
  it("renders all four sections", async () => {
    showRoute("#/dashboard");
    await flush();
    for (const heading of ["Application health", "Engines", "Job statistics", "Recent jobs", "Target instances"]) {
      assert.ok(containing(root(), heading), `missing section: ${heading}`);
    }
  });

  it("keeps exactly one simulation-only notice, owned by the shell", async () => {
    showRoute("#/dashboard");
    await flush();
    const banner = assertLoneShellBanner(globalThis.document);
    assert.equal(banner.getAttribute("id"), "simulation-notice");
    assert.match(banner.textContent, /Simulation only/);
    assert.doesNotMatch(
      root().textContent,
      /Simulation only/,
      "the dashboard must not duplicate the shell banner"
    );
  });

  it("renders four statistic cards from GET /api/jobs/stats", async () => {
    showRoute("#/dashboard");
    await flush();
    const cards = root().querySelectorAll(".stat");
    assert.equal(cards.length, 4, "the summary row must contain four cards");
    const rendered = Object.fromEntries(
      cards.map((card) => [
        card.querySelector(".stat__label").textContent,
        card.querySelector(".stat__value").textContent,
      ])
    );
    assert.deepEqual(rendered, {
      "Total jobs": "2",
      Simulated: "1",
      Denied: "1",
      Failed: "0",
    });
  });

  it("formats job timestamps instead of printing raw epochs", async () => {
    showRoute("#/dashboard");
    await flush();
    const text = root().textContent;
    assert.ok(
      text.includes(formatTimestamp(1700000001)),
      "the created column must use the shared timestamp helper"
    );
    assert.doesNotMatch(text, /1700000001/, "no raw epoch may reach the dashboard");
    assert.doesNotMatch(text, /1700000002/, "no raw epoch may reach the dashboard");
  });

  it("shows zeroed statistic cards when the stats payload is empty", async () => {
    start({ "GET /api/jobs/stats": { status: 200, body: { total: 0, simulated: 0, denied: 0, failed: 0 } } });
    showRoute("#/dashboard");
    await flush();
    const values = root()
      .querySelectorAll(".stat__value")
      .map((el) => el.textContent);
    assert.deepEqual(values, ["0", "0", "0", "0"]);
  });

  it("shows the operating mode from GET /health", async () => {
    showRoute("#/dashboard");
    await flush();
    assert.match(root().textContent, /mode: simulated/);
  });

  it("requests exactly the five dashboard endpoints", async () => {
    showRoute("#/dashboard");
    await flush();
    assert.deepEqual(
      [...new Set(env.calls.map((c) => c.url))].sort(),
      ["/api/engines", "/api/jobs", "/api/jobs/stats", "/api/target-instances", "/health"]
    );
  });

  it("renders real engine capability fields", async () => {
    showRoute("#/dashboard");
    await flush();
    const section = containing(root(), "Engines");
    assert.match(section.textContent, /john/);
    assert.match(section.textContent, /verified/);
  });

  it("renders job and instance summaries", async () => {
    showRoute("#/dashboard");
    await flush();
    assert.match(root().textContent, /2 recorded job/);
    assert.match(root().textContent, /1 target instance/);
  });

  it("shows at most five recent jobs", async () => {
    start({
      "GET /api/jobs": {
        status: 200,
        body: {
          jobs: Array.from({ length: 9 }, (_, i) => ({
            job_id: `id-${i}`,
            status: "simulated",
            created_at: 1700000000 + i,
            target: "pdf-document",
          })),
          count: 9,
        },
      },
    });
    showRoute("#/dashboard");
    await flush();
    assert.match(root().textContent, /Showing 5 of 9/);
  });
});

describe("dashboard - independent section failure", () => {  it("one failing section leaves the other three rendered", async () => {
    start({ "GET /api/jobs": { status: 500, body: { detail: "boom" } } });
    showRoute("#/dashboard");
    await flush();

    const jobs = containing(root(), "Recent jobs");
    assert.match(jobs.textContent, /This section failed to load/);

    const text = root().textContent;
    assert.match(text, /mode: simulated/, "health must still render");
    assert.match(text, /john/, "engines must still render");
    assert.match(text, /1 target instance/, "instances must still render");
  });

  it("a failing engines section leaves jobs and instances rendered", async () => {
    start({ "GET /api/engines": { status: 502, body: { detail: "bad gateway" } } });
    showRoute("#/dashboard");
    await flush();
    assert.match(containing(root(), "Engines").textContent, /This section failed to load/);
    assert.match(root().textContent, /2 recorded job/);
  });

  it("a failing instances section leaves engines and jobs rendered", async () => {
    start({ "GET /api/target-instances": { status: 500, body: { detail: "boom" } } });
    showRoute("#/dashboard");
    await flush();
    assert.match(root().textContent, /john/);
    assert.match(root().textContent, /2 recorded job/);
  });

  it("a transport failure is reported and does not break siblings", async () => {
    start();
    stub({
      "/api/jobs": () => {
        throw new Error("connection refused");
      },
      "/health": { status: 200, body: HEALTH },
      "/api/engines": { status: 200, body: ENGINES },
      "/api/target-instances": { status: 200, body: INSTANCES },
    });
    showRoute("#/dashboard");
    await flush();
    const jobs = containing(root(), "Recent jobs");
    assert.match(jobs.textContent, /Cannot reach the PassOrch API|This section failed/);
    assert.match(root().textContent, /john/);
  });
});

describe("dashboard - retry", () => {
  it("the failed section offers a retry that re-renders it", async () => {
    start({ "GET /api/engines": { status: 500, body: { detail: "boom" } } });
    showRoute("#/dashboard");
    await flush();

    const engines = containing(root(), "Engines");
    const retry = engines
      .querySelectorAll("button")
      .find((b) => /Retry/.test(b.textContent));
    assert.ok(retry, "a failed section must offer retry");

    stub({
      "/api/engines": { status: 200, body: ENGINES },
      "/health": { status: 200, body: HEALTH },
      "/api/jobs": { status: 200, body: JOBS },
      "/api/jobs/stats": { status: 200, body: { total: 2, simulated: 1, denied: 1, failed: 0, by_status: { simulated: 1, denied: 1 }, by_target: { "pdf-document": 1, "zip-archive": 1 } } },
      "/api/target-instances": { status: 200, body: INSTANCES },
    });

    retry.dispatchEvent({ type: "click", target: retry });
    await flush(12);

    const refreshed = containing(root(), "Engines");
    assert.match(refreshed.textContent, /john/, "retry must re-render the section");
    assert.doesNotMatch(refreshed.textContent, /This section failed to load/);
  });

  it("the retry handler is scoped to its own section", async () => {
    start({
      "GET /api/engines": { status: 500, body: { detail: "boom" } },
      "GET /api/jobs/stats": { status: 200, body: { total: 0, simulated: 0, denied: 0, failed: 0, by_status: {}, by_target: {} } },
    });
    showRoute("#/dashboard");
    await flush();
    const engines = containing(root(), "Engines");
    const retries = engines
      .querySelectorAll("button")
      .filter((b) => /Retry/.test(b.textContent));
    assert.equal(retries.length, 1, "the failed section offers exactly one retry");
  });
});

describe("dashboard - honest empty states", () => {
  it("explains an empty engine list and invents nothing", async () => {
    start({ "GET /api/engines": { status: 200, body: { engines: [], } } });
    showRoute("#/dashboard");
    await flush();
    const engines = containing(root(), "Engines");
    assert.match(engines.textContent, /No engine capability data available/);
    assert.match(engines.textContent, /never triggers discovery/i);
    assert.doesNotMatch(engines.textContent, /\bjohn\b/);
  });

  it("links the empty job state to the Jobs screen", async () => {
    start({ "GET /api/jobs": { status: 200, body: { jobs: [], count: 0 } } });
    showRoute("#/dashboard");
    await flush();
    const jobs = containing(root(), "Recent jobs");
    assert.match(jobs.textContent, /No jobs recorded yet/);
    assert.ok(jobs.querySelectorAll("a").some((a) => a.getAttribute("href") === "#/jobs"));
  });

  it("links the empty instance state to the Instances screen", async () => {
    start({ "GET /api/target-instances": { status: 200, body: { instances: [], count: 0 } } });
    showRoute("#/dashboard");
    await flush();
    const inst = containing(root(), "Target instances");
    assert.match(inst.textContent, /No target instances defined/);
    assert.ok(inst.querySelectorAll("a").some((a) => a.getAttribute("href") === "#/instances"));
  });
});

describe("formatTimestamp", () => {
  it("renders epoch seconds as a human-readable date and time", () => {
    const formatted = formatTimestamp(1700000001);
    assert.notEqual(formatted, "-");
    assert.notEqual(formatted, "1700000001");
    assert.ok(formatted.length > 4, "a formatted date must not be a bare number");
  });

  it("accepts the numeric string form the API may serialize", () => {
    assert.equal(formatTimestamp("1700000001"), formatTimestamp(1700000001));
  });

  it("returns a dash for missing, blank, boolean or non-numeric values", () => {
    for (const value of [undefined, null, "", "   ", true, false, "nope", NaN, Infinity, {}, []]) {
      assert.equal(formatTimestamp(value), "-", `unexpected formatting for ${String(value)}`);
    }
  });

  it("does not throw on an out-of-range epoch", () => {
    assert.doesNotThrow(() => formatTimestamp(Number.MAX_SAFE_INTEGER));
  });
});
