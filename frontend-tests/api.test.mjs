import assert from "node:assert/strict";
import { describe, it, beforeEach } from "node:test";

import { describeError, request, api, API_ERRORS } from "../frontend/js/api.js";
import { FakeResponse, flush } from "./_fixtures.mjs";

const OBJECT_ERROR = {
  detail: {
    error: "unknown_target",
    target: "nope",
    detail: "target is not mapped to an enabled format policy",
    supported_targets: ["pdf-document"],
  },
};

const ARRAY_ERROR = {
  detail: [
    { type: "missing", loc: ["body", "target"], msg: "Field required" },
    { type: "extra_forbidden", loc: ["body", "command"], msg: "Extra inputs are not permitted" },
  ],
};

beforeEach(() => {
  delete globalThis.fetch;
});

describe("api.js - successful responses", () => {
  it("parses a JSON body and reports success", async () => {
    globalThis.fetch = async () => new FakeResponse(200, { status: "ok", mode: "simulated" });
    const res = await api.get("/health");
    assert.equal(res.ok, true);
    assert.equal(res.status, 200);
    assert.equal(res.error, null);
    assert.equal(res.data.mode, "simulated");
  });

  it("treats 201 as success", async () => {
    globalThis.fetch = async () => new FakeResponse(201, { id: "abc" });
    const res = await api.post("/api/target-instances", { name: "n" });
    assert.equal(res.ok, true);
    assert.equal(res.status, 201);
    assert.equal(res.data.id, "abc");
  });

  it("tolerates an empty body on a 200", async () => {
    globalThis.fetch = async () => new FakeResponse(200, "");
    const res = await api.get("/api/jobs");
    assert.equal(res.ok, true);
    assert.equal(res.data, null);
    assert.equal(res.parseError, false);
  });
});

describe("api.js - HTTP 204 No Content", () => {
  it("reports success with no body", async () => {
    globalThis.fetch = async () => new FakeResponse(204, undefined);
    const res = await api.delete("/api/target-instances/abc");
    assert.equal(res.ok, true);
    assert.equal(res.status, 204);
    assert.equal(res.data, null);
    assert.equal(res.error, null);
  });
});

describe("api.js - error payload shapes", () => {
  it("normalises an object-shaped domain error", async () => {
    globalThis.fetch = async () => new FakeResponse(422, OBJECT_ERROR);
    const res = await api.post("/api/jobs", { target: "nope" });
    assert.equal(res.ok, false);
    assert.equal(res.status, 422);
    assert.equal(res.error.kind, "http");
    assert.equal(res.error.code, "unknown_target");
    assert.match(res.error.message, /unknown_target/);
  });

  it("normalises an array-shaped Pydantic validation error", async () => {
    globalThis.fetch = async () => new FakeResponse(422, ARRAY_ERROR);
    const res = await api.post("/api/jobs", { target: "pdf-document", command: "x" });
    assert.equal(res.ok, false);
    assert.match(res.error.message, /Validation failed/);
    assert.match(res.error.message, /body\.target/);
    assert.match(res.error.message, /body\.command/);
    assert.equal(res.error.code, "");
  });

  it("passes a plain string detail through", async () => {
    globalThis.fetch = async () => new FakeResponse(404, { detail: "job_not_found" });
    const res = await api.get("/api/jobs/does-not-exist");
    assert.equal(res.ok, false);
    assert.equal(res.status, 404);
    assert.equal(res.error.message, "job_not_found");
  });

  it("keeps a malformed error body out of the user-facing message", async () => {
    globalThis.fetch = async () => new FakeResponse(500, "<html>bad gateway</html>");
    const res = await api.get("/api/jobs");
    assert.equal(res.ok, false);
    assert.equal(res.status, 500);
    assert.equal(res.error.code, "");
    assert.doesNotMatch(res.error.message, /bad gateway/);
    // the raw text is retained for diagnostics only
    assert.equal(res.raw, "<html>bad gateway</html>");
  });

  it("degrades a null detail to a status line", async () => {
    globalThis.fetch = async () => new FakeResponse(500, { detail: null });
    const res = await api.get("/api/jobs");
    assert.match(res.error.message, /500/);
  });

  it("exposes the documented domain error codes", () => {
    assert.equal(API_ERRORS.unknownTarget, "unknown_target");
    assert.equal(API_ERRORS.instanceNotFound, "target_instance_not_found");
    assert.equal(API_ERRORS.instanceTypeMismatch, "target_instance_type_mismatch");
  });
});

describe("api.js - transport failures", () => {
  it("distinguishes a transport failure from an HTTP error", async () => {
    globalThis.fetch = async () => {
      throw new Error("connection refused");
    };
    const res = await api.get("/api/jobs");
    assert.equal(res.ok, false);
    assert.equal(res.status, 0);
    assert.equal(res.error.kind, "transport");
    assert.equal(res.data, null);
    assert.equal(res.data, null);
  });
});

describe("api.js - first-party path guard", () => {
  it("refuses an absolute origin without calling fetch", async () => {
    let called = false;
    globalThis.fetch = async () => {
      called = true;
      return new FakeResponse(200, {});
    };
    const res = await request("http://evil.test/api/jobs");
    assert.equal(res.ok, false);
    assert.equal(res.status, 0);
    assert.equal(res.error.kind, "invalid_request");
    assert.equal(called, false, "a rejected URL must never reach fetch");
  });

  it("refuses a non-API relative path", async () => {
    let called = false;
    globalThis.fetch = async () => {
      called = true;
      return new FakeResponse(200, {});
    };
    const res = await request("/etc/passwd");
    assert.equal(res.error.kind, "invalid_request");
    assert.equal(called, false);
  });

  it("allows /health and /api/ paths", async () => {
    for (const path of ["/health", "/api/jobs", "/api/targets"]) {
      globalThis.fetch = async () => new FakeResponse(200, { ok: true });
      const res = await request(path);
      assert.equal(res.ok, true, path);
    }
  });
});

describe("api.js - request construction", () => {
  it("sends JSON with the right method and content type", async () => {
    let seen;
    globalThis.fetch = async (url, opts) => {
      seen = { url, opts };
      return new FakeResponse(200, {});
    };
    await api.post("/api/jobs", { target: "pdf-document" });
    assert.equal(seen.opts.method, "POST");
    assert.equal(seen.opts.headers["Content-Type"], "application/json");
    assert.deepEqual(JSON.parse(seen.opts.body), { target: "pdf-document" });
  });

  it("omits instance_id from the body when it is absent", async () => {
    let body;
    globalThis.fetch = async (_u, opts) => {
      body = JSON.parse(opts.body);
      return new FakeResponse(200, {});
    };
    const payload = { target: "pdf-document" };
    if ("x") {
      payload.instance_id = "x";
    }
    delete payload.instance_id;
    await api.post("/api/jobs", payload);
    assert.equal("instance_id" in body, false);
  });

  it("propagates an AbortSignal", async () => {
    const controller = new AbortController();
    let seen;
    globalThis.fetch = async (_u, opts) => {
      seen = opts;
      return new FakeResponse(200, {});
    };
    await api.get("/api/jobs", controller.signal);
    assert.equal(seen.signal, controller.signal);
  });
});

describe("describeError (pure)", () => {
  it("joins validation locations with their messages", () => {
    const msg = describeError(422, [{ loc: ["body", "target"], msg: "required" }]);
    assert.match(msg, /Validation failed/);
    assert.match(msg, /body\.target: required/);
  });

  it("returns the string detail verbatim", () => {
    assert.equal(describeError(404, "job_not_found"), "job_not_found");
  });
});

describe("state isolation", () => {
  it("drains queued promise work between tests", async () => {
    await flush();
    globalThis.fetch = async () => new FakeResponse(200, { a: 1 });
    const res = await api.get("/api/targets");
    assert.equal(res.ok, true);
  });
});
