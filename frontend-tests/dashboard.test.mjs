import assert from "node:assert/strict";
import { describe, it, beforeEach } from "node:test";

import { showRoute } from "../frontend/js/app.js";
import "../frontend/js/views/dashboard.js";
import { FakeResponse, containing, flush, setup } from "./_fixtures.mjs";

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
function start(overrides) {
  env = setup({
    "GET /health": { status: 200, body: HEALTH },
    "GET /api/engines": { status: 200, body: ENGINES },
    "GET /api/jobs": { status: 200, body: JOBS },
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
    for (const heading of ["Application health", "Engines", "Recent jobs", "Target instances"]) {
      assert.ok(containing(root(), heading), `missing section: ${heading}`);
    }
  });

  it("keeps the simulation-only notice visible", async () => {
    showRoute("#/dashboard");
    await flush();
    assert.match(root().textContent, /Simulation only/);
  });

  it("shows the operating mode from GET /health", async () => {
    showRoute("#/dashboard");
    await flush();
    assert.match(root().textContent, /mode: simulated/);
  });

  it("requests exactly the four dashboard endpoints", async () => {
    showRoute("#/dashboard");
    await flush();
    assert.deepEqual(
      [...new Set(env.calls.map((c) => c.url))].sort(),
      ["/api/engines", "/api/jobs", "/api/target-instances", "/health"]
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

describe("dashboard - independent section failure", () => {
  it("one failing section leaves the other three rendered", async () => {
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
      "/api/target-instances": { status: 200, body: INSTANCES },
    });

    retry.dispatchEvent({ type: "click", target: retry });
    await flush(12);

    const refreshed = containing(root(), "Engines");
    assert.match(refreshed.textContent, /john/, "retry must re-render the section");
    assert.doesNotMatch(refreshed.textContent, /This section failed to load/);
  });

  it("the retry handler is scoped to its own section", async () => {
    start({ "GET /api/engines": { status: 500, body: { detail: "boom" } } });
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
