import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { installFetch, isEnvelope } from "./_fixtures.mjs";

const JOB = { job_id: "x", status: "simulated", target: "pdf-document" };

describe("fixture: envelope and wildcard resolution", () => {
  it("resolves an explicit {status, body} envelope", async () => {
    const { fetch } = installFetch({ "POST /api/jobs": { status: 201, body: { id: "y" } } });
    const res = await fetch("/api/jobs", { method: "POST", body: {} });
    assert.equal(res.status, 201);
    assert.deepEqual(JSON.parse(await res.text()), { id: "y" });
  });

  it("resolves a callable spec lazily", async () => {
    let calls = 0;
    const { fetch } = installFetch({
      "POST /api/jobs": () => {
        calls += 1;
        return { status: 422, body: { detail: { error: "unknown_target" } } };
      },
    });
    const res = await fetch("/api/jobs", { method: "POST", body: {} });
    assert.equal(res.status, 422);
    assert.equal(calls, 1);
  });

  it("resolves a bare status number to an empty body", async () => {
    const { fetch } = installFetch({ "DELETE /api/target-instances/*": 204 });
    const res = await fetch("/api/target-instances/abc", { method: "DELETE" });
    assert.equal(res.status, 204);
    assert.equal(await res.text(), "");
  });

  it("resolves a raw string spec to implied 200", async () => {
    const { fetch } = installFetch({ "GET /api/jobs/abc": "<html>oops</html>" });
    const res = await fetch("/api/jobs/abc");
    assert.equal(res.status, 200);
    assert.equal(await res.text(), "<html>oops</html>");
  });

  it("falls back to 404 for an unscripted route", async () => {
    const { fetch } = installFetch({});
    const res = await fetch("/api/unknown");
    assert.equal(res.status, 404);
    assert.deepEqual(JSON.parse(await res.text()), { detail: "job_not_found" });
  });

  it("records the outgoing method and parsed body", async () => {
    const { calls, fetch } = installFetch({ "POST /api/jobs": { status: 200, body: {} } });
    await fetch("/api/jobs", { method: "POST", body: JSON.stringify({ target: "pdf" }) });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "POST");
    assert.deepEqual(calls[0].body, { target: "pdf" });
  });

  it("refuses an ambiguous spec instead of guessing", async () => {
    const { fetch } = installFetch({ "POST /api/jobs": JOB });
    await assert.rejects(
      () => fetch("/api/jobs", { method: "POST", body: {} }),
      /ambiguous response spec/
    );
  });

  it("classifies envelopes by a numeric status", () => {
    assert.equal(isEnvelope({ status: 200, body: JOB }), true);
    assert.equal(isEnvelope(JOB), false);
    assert.equal(isEnvelope({ status: "simulated" }), false);
    assert.equal(isEnvelope(204), false);
    assert.equal(isEnvelope(null), false);
  });
});
