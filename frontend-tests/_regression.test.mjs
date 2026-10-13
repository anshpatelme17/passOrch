import assert from "node:assert/strict";
import { beforeEach, describe, it } from "node:test";

import { api, describeError } from "../frontend/js/api.js";
import { ENVELOPE, FakeResponse, installFetch, isEnvelope } from "./_fixtures.mjs";

/**
 * A job record has a domain `status` field ("simulated" / "denied") -- a
 * STRING. An HTTP envelope has a numeric `status`. These must never be
 * confused: treating a job body as an envelope would report HTTP 200 with a
 * nonsense detail, and every downstream view would misrender the job.
 */
const SIMULATED_JOB = {
  job_id: "11111111-1111-4111-8111-111111111111",
  status: "simulated",
  created_at: 1700000010,
  target: "pdf-document",
  failure_reason: "",
  instance_id: "",
  instance_target_type: "",
  preview: {
    target: "pdf-document",
    selection: { status: "selected", reason: "verified_capability", selected_engine: "john", candidates: ["john"] },
    request: {
      target: "pdf-document",
      engine: "mock_john",
      mode: "simulated",
      selection_status: "selected",
      capability_status: "verified",
      selected_engine: "john",
      matched_formats: ["pdf"],
      reference: "",
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

const DENIED_JOB = {
  ...SIMULATED_JOB,
  job_id: "22222222-2222-4222-8222-222222222222",
  status: "denied",
  failure_reason: "capability_not_verified",
  instance_id: "",
  instance_target_type: "",
  preview: { ...SIMULATED_JOB.preview, authorization: { ...SIMULATED_JOB.preview.authorization, authorized: false, status: "denied", reason: "capability_not_verified" }, plan: null, simulation: null },
};

describe("fixture: envelope detection (regression)", () => {
  it("does not treat a job record as an envelope", async () => {
    const { fetch } = installFetch({
      "POST /api/jobs": { status: 200, body: SIMULATED_JOB },
    });
    const res = await fetch("/api/jobs", { method: "POST", body: {} });
    assert.equal(res.status, 200, "a job body must not become the HTTP status");
    assert.equal(res.ok, true);
  });

  it("treats {status: 200, body: <job>} as an envelope with HTTP 200", async () => {
    const { fetch } = installFetch({
      "POST /api/jobs": { status: 200, body: SIMULATED_JOB },
    });
    const res = await fetch("/api/jobs", { method: "POST", body: {} });
    const parsed = await api
      .post("/api/jobs", { target: "pdf-document" })
      .catch(() => null);
    void parsed;
    assert.equal(res.status, 200);
  });

  it("refuses a bare body object instead of guessing", async () => {
    // the old ambiguous form is refused loudly when the request is made,
    // rather than silently reinterpreted
    const { fetch } = installFetch({ "POST /api/jobs": SIMULATED_JOB });
    await assert.rejects(
      () => fetch("/api/jobs", { method: "POST", body: {} }),
      /ambiguous response spec/
    );
  });

  it("distinguishes domain status (string) from HTTP status (number)", () => {
    assert.equal(typeof SIMULATED_JOB.status, "string");
    assert.equal(isEnvelope({ status: 200, body: SIMULATED_JOB }), true);
    assert.equal(isEnvelope(SIMULATED_JOB), false, "a job body is not an envelope");
    assert.equal(isEnvelope({ status: "simulated" }), false);
    assert.equal(ENVELOPE, "status");
  });
});

describe("api.js: real job payloads round-trip through the real client", () => {
  beforeEach(() => {
    delete globalThis.fetch;
  });

  it("a simulated job is HTTP 200 with status simulated", async () => {
    globalThis.fetch = async () => new FakeResponse(200, SIMULATED_JOB);
    const res = await api.post("/api/jobs", { target: "pdf-document" });
    assert.equal(res.ok, true);
    assert.equal(res.status, 200);
    assert.equal(res.data.status, "simulated");
    assert.equal(res.data.failure_reason, "");
    assert.equal(res.data.preview.plan.simulated, true);
    assert.equal(res.data.preview.simulation.simulated, true);
  });

  it("a denied job is HTTP 200 with status denied and no plan", async () => {
    globalThis.fetch = async () => new FakeResponse(200, DENIED_JOB);
    const res = await api.post("/api/jobs", { target: "pdf-document" });
    assert.equal(res.ok, true, "a denial is HTTP 200, not an error");
    assert.equal(res.status, 200);
    assert.equal(res.data.status, "denied");
    assert.equal(res.data.failure_reason, "capability_not_verified");
    assert.equal(res.data.preview.plan, null);
    assert.equal(res.data.preview.simulation, null);
    assert.equal(res.data.instance_id, "");
    assert.equal(res.data.instance_target_type, "");
  });

  it("a job record with an association carries the snapshot", async () => {
    const job = {
      ...SIMULATED_JOB,
      instance_id: "33333333-3333-4333-8333-333333333333",
      instance_target_type: "pdf-document",
    };
    globalThis.fetch = async () => new FakeResponse(200, job);
    const res = await api.post("/api/jobs", { target: "pdf-document" });
    assert.equal(res.data.instance_id, "33333333-3333-4333-8333-333333333333");
    assert.equal(res.data.instance_target_type, "pdf-document");
  });

  it("a job list envelope is HTTP 200 with a jobs array", async () => {
    const listing = {
      status: 200,
      body: {
        jobs: [
          { job_id: "a", status: "simulated", created_at: 1, target: "pdf-document" },
          { job_id: "b", status: "denied", created_at: 2, target: "zip-archive" },
        ],
        count: 2,
      },
    };
    globalThis.fetch = async () => new FakeResponse(200, listing.body);
    const res = await api.get("/api/jobs");
    assert.equal(res.ok, true);
    assert.equal(res.status, 200);
    assert.equal(res.data.count, 2);
    assert.deepEqual(
      res.data.jobs.map((j) => j.status),
      ["simulated", "denied"]
    );
    // JobSummary carries no instance_id
    assert.equal("instance_id" in res.data.jobs[0], false);
  });

  it("a detail 404 is an error, not a job", async () => {
    globalThis.fetch = async () => new FakeResponse(404, { detail: "job_not_found" });
    const res = await api.get("/api/jobs/does-not-exist");
    assert.equal(res.ok, false);
    assert.equal(res.status, 404);
    assert.equal(res.error.message, "job_not_found");
  });

  it("describeError never leaks a job body as the detail", () => {
    const msg = describeError(500, { status: "simulated" });
    assert.doesNotMatch(msg, /simulated/);
  });
});
