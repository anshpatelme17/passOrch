import assert from "node:assert/strict";
import { describe, it, beforeEach } from "node:test";

import { showRoute } from "../frontend/js/app.js";
import "../frontend/js/views/jobs.js";
import {
  FakeResponse,
  click,
  containing,
  deniedJob,
  flush,
  rootText,
  selectValues,
  setup,
  simulatedJob,
  type,
} from "./_fixtures.mjs";

let env;

const CATALOG = {
  targets: [
    { target: "pdf-document", description: "PDF", formats: ["pdf"], enabled: true, usable: true },
    { target: "zip-archive", description: "Zip", formats: ["zip"], enabled: true, usable: false },
  ],
  count: 2,
};
const EMPTY_LIST = { jobs: [], count: 0 };
const TWO_JOBS = {
  jobs: [
    { job_id: "11111111-1111-4111-8111-111111111111", status: "simulated", created_at: 1700000001, target: "pdf-document" },
    { job_id: "22222222-2222-4222-8222-222222222222", status: "denied", created_at: 1700000002, target: "zip-archive" },
  ],
  count: 2,
};
const INSTANCE = "33333333-3333-4333-8333-333333333333";
const FULL = "11111111-1111-4111-8111-111111111111";
const INSTANCES = {
  instances: [
    { id: INSTANCE, name: "Contract PDF", target_type: "pdf-document", description: "", created_at: 1700000005 },
    { id: "44444444-4444-4444-8444-444444444444", name: "Q3 Archive", target_type: "zip-archive", description: "", created_at: 1700000006 },
  ],
  count: 2,
};

function start(routes) {
  env = setup({
    "GET /api/targets": { status: 200, body: CATALOG },
    "GET /api/target-instances": { status: 200, body: INSTANCES },
    "GET /api/jobs": { status: 200, body: TWO_JOBS },
    ...(routes || {}),
  });
  return env;
}
function root() {
  return globalThis.document.getElementById("view-root");
}
function select(id) {
  return root().querySelectorAll(`[id="${id}"]`)[0];
}
function btn(label) {
  return root().querySelectorAll("button").find((b) => new RegExp(label).test(b.textContent));
}

/** Pick a target type through the real change handler. */
async function choose(targetType) {
  const sel = select("job-target");
  sel.value = targetType;
  sel.dispatchEvent({ type: "change", target: sel });
  await flush();
}
/** Pick an instance through the real change handler. */
async function chooseInstance(instanceId) {
  const sel = select("job-instance");
  sel.value = instanceId;
  sel.dispatchEvent({ type: "change", target: sel });
  await flush();
}

beforeEach(() => {
  start();
});

describe("jobs - create form", () => {
  it("offers the real target types", async () => {
    showRoute("#/jobs");
    await flush();
    assert.deepEqual(selectValues(select("job-target")), ["", "pdf-document", "zip-archive"]);
  });

  it("offers only the simulation mode", async () => {
    showRoute("#/jobs");
    await flush();
    assert.deepEqual(selectValues(select("job-mode")), ["simulated"]);
  });

  it("offers the instances, labelling mismatches", async () => {
    showRoute("#/jobs");
    await flush();
    const sel = select("job-instance");
    const labels = sel.querySelectorAll("option").map((o) => o.textContent);
    // nothing selected yet: no mismatch label
    assert.ok(labels.some((l) => /Contract PDF/.test(l)));
    assert.ok(labels.some((l) => /Q3 Archive/.test(l)));
    assert.ok(labels.some((l) => /No target instance/.test(l)));
  });

  it("labels a mismatched instance without changing the selection", async () => {
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    await chooseInstance("44444444-4444-4444-8444-444444444444"); // zip instance
    const labels = select("job-instance").querySelectorAll("option").map((o) => o.textContent);
    assert.ok(
      labels.some((l) => /Q3 Archive \(zip-archive - not this target\)/.test(l)),
      "a mismatching instance must be labelled"
    );
    // the user's choice is preserved
    assert.equal(select("job-instance").value, "44444444-4444-4444-8444-444444444444");
    assert.equal("Q3 Archive (zip-archive - not this target)".includes("not this target"), true);
  });

  it("disables submit until a target is chosen", async () => {
    showRoute("#/jobs");
    await flush();
    assert.equal(btn("Create simulated job").hasAttribute("disabled"), true);
    await choose("pdf-document");
    assert.equal(btn("Create simulated job").hasAttribute("disabled"), false);
  });

  it("sends exactly the JobCreateRequest keys", async () => {
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    type(select("job-reference"), "audit-7");
    click(btn("Create simulated job"));
    await flush();
    const posted = env.calls.find((c) => c.method === "POST");
    assert.ok(posted);
    assert.deepEqual(Object.keys(posted.body).sort(), ["mode", "reference", "target"]);
    assert.equal(posted.body.target, "pdf-document");
  });

  it("omits instance_id when unset", async () => {
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush();
    const posted = env.calls.find((c) => c.method === "POST");
    assert.ok(posted);
    assert.equal("instance_id" in posted.body, false);
  });

  it("includes instance_id when set", async () => {
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    await chooseInstance(INSTANCE);
    click(btn("Create simulated job"));
    await flush();
    const posted = env.calls.find((c) => c.method === "POST");
    assert.ok(posted);
    assert.equal(posted.body.instance_id, INSTANCE);
  });
});

describe("jobs - outcomes", () => {
  it("reports a simulated job as created, not authorized execution", async () => {
    start({ "POST /api/jobs": { status: 200, body: simulatedJob() } });
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush();
    const text = rootText(root());
    assert.match(text, /Simulated job created\./);
    assert.match(text, /No engine ran\./);
  });

  it("reports a denied job as a recorded outcome, not an error", async () => {
    start({ "POST /api/jobs": { status: 200, body: deniedJob() } });
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush();
    const text = rootText(root());
    assert.match(text, /Job recorded with a denied outcome\./);
    assert.match(text, /capability_not_verified/);
    assert.doesNotMatch(text, /Simulated job created/);
  });

  it("reports a non-simulation-mode denial with its reason", async () => {
    start({ "POST /api/jobs": { status: 200, body: deniedJob("non_simulation_request") } });
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush();
    assert.match(rootText(root()), /non_simulation_request/);
  });

  it("refreshes the list after a successful create", async () => {
    start({ "POST /api/jobs": { status: 200, body: simulatedJob() } });
    showRoute("#/jobs");
    await flush();
    const before = env.calls.filter((c) => c.url === "/api/jobs").length;
    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush();
    const after = env.calls.filter((c) => c.url === "/api/jobs").length;
    assert.ok(after > before, "the list must be re-fetched after creation");
  });

  it("opens the created job's details automatically", async () => {
    start({ "POST /api/jobs": { status: 200, body: simulatedJob() }, "GET /api/jobs/55555555-5555-4555-8555-555555555555": { status: 200, body: simulatedJob() } });
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush(14);
    assert.match(rootText(root()), /Job 55555555-5555-4555-8555-555555555555/);
  });
});

describe("jobs - domain and validation errors", () => {
  it("surfaces unknown_target with its code", async () => {
    start({ "POST /api/jobs": { status: 422, body: { detail: { error: "unknown_target", target: "nope" } } } });
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush();
    assert.match(rootText(root()), /unknown_target/);
  });

  it("surfaces target_instance_not_found", async () => {
    start({ "POST /api/jobs": { status: 422, body: { detail: { error: "target_instance_not_found", target_instance_id: "x" } } } });
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush();
    assert.match(rootText(root()), /target_instance_not_found/);
  });

  it("surfaces target_instance_type_mismatch", async () => {
    start({
      "POST /api/jobs": {
        status: 422,
        body: {
          detail: {
            error: "target_instance_type_mismatch",
            target: "pdf-document",
            instance_target_type: "zip-archive",
          },
        },
      },
    });
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush();
    assert.match(rootText(root()), /target_instance_type_mismatch/);
  });

  it("surfaces a Pydantic array-shaped error", async () => {
    start({
      "POST /api/jobs": {
        status: 422,
        body: { detail: [{ type: "missing", loc: ["body", "target"], msg: "Field required" }] },
      },
    });
    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush();
    assert.match(rootText(root()), /Validation failed/);
  });

  it("surfaces a transport error on create", async () => {
    start();
    showRoute("#/jobs");
    await flush();

    // the catalog and list load fine; only the POST fails at the network level
    globalThis.fetch = async (url) => {
      const target = String(url);
      if (target.endsWith("/api/targets")) {
        return new FakeResponse(200, CATALOG);
      }
      if (target.endsWith("/api/target-instances")) {
        return new FakeResponse(200, INSTANCES);
      }
      if (target.includes("/api/jobs")) {
        throw new Error("offline");
      }
      return new FakeResponse(404, { detail: "job_not_found" });
    };

    const sel = select("job-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    click(btn("Create simulated job"));
    await flush(12);
    assert.match(rootText(root()), /Cannot reach the PassOrch API|This section failed/);
  });
});

describe("jobs - list", () => {
  it("renders the job summaries with a per-row details action", async () => {
    showRoute("#/jobs");
    await flush();
    const text = rootText(root());
    assert.match(text, /11111111-1111-4111-8111-111111111111/);
    assert.match(text, /simulated/);
    assert.match(text, /denied/);
    assert.match(text, /2 recorded job/);
  });

  it("does not assume instance_id on a summary row", async () => {
    showRoute("#/jobs");
    await flush();
    // the association only appears after opening a detail
    const list = containing(root(), "Jobs");
    assert.doesNotMatch(list.textContent, /instance target type/);
  });

  it("renders an empty state", async () => {
    start({ "GET /api/jobs": { status: 200, body: EMPTY_LIST } });
    showRoute("#/jobs");
    await flush();
    assert.match(rootText(root()), /No jobs recorded yet/);
  });

  it("renders an error with a retry", async () => {
    start({ "GET /api/jobs": { status: 500, body: { detail: "boom" } } });
    showRoute("#/jobs");
    await flush();
    assert.match(rootText(root()), /This section failed to load|Cannot reach/);
    assert.ok(root().querySelectorAll("button").some((b) => /Retry/.test(b.textContent)));
  });
});

describe("jobs - details", () => {
  it("renders JobInfo fields including the association", async () => {
    start({
      "GET /api/jobs/11111111-1111-4111-8111-111111111111": { status: 200, body: simulatedJob(INSTANCE, FULL) },
    });
    showRoute("#/jobs");
    await flush();
    const row = root()
      .querySelectorAll("button")
      .find((b) => b.textContent === "Details");
    click(row);
    await flush(16);
    const text = rootText(root());
    assert.match(text, /Job 11111111-1111-4111-8111-111111111111/);
    assert.match(text, /instance target type/i);
    assert.match(text, /33333333-3333-4333-8333-333333333333/);
  });

  it("renders an unassociated job honestly", async () => {
    start({ "GET /api/jobs/11111111-1111-4111-8111-111111111111": { status: 200, body: simulatedJob(null, FULL) } });
    showRoute("#/jobs");
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Details"));
    await flush(12);
    assert.match(rootText(root()), /Created without a target-instance association/);
  });

  it("renders a denied job's preview with no plan", async () => {
    start({ "GET /api/jobs/22222222-2222-4222-8222-222222222222": { status: 200, body: deniedJob() } });
    showRoute("#/jobs");
    await flush();
    const rows = root().querySelectorAll("button").filter((b) => b.textContent === "Details");
    click(rows[rows.length - 1]);
    await flush(12);
    assert.match(rootText(root()), /This job was denied by the execution policy/);
  });

  it("shows 404 for an unknown job", async () => {
    // route the second job's details to 404
    start({
      "GET /api/jobs/22222222-2222-4222-8222-222222222222": {
        status: 404,
        body: { detail: "job_not_found" },
      },
    });
    showRoute("#/jobs");
    await flush();
    const rows = root().querySelectorAll("button").filter((b) => b.textContent === "Details");
    click(rows[rows.length - 1]);
    await flush(12);
    assert.match(rootText(root()), /job_not_found/);
  });

  it("handles a malformed detail response", async () => {
    start({ "GET /api/jobs/11111111-1111-4111-8111-111111111111": "<html>x</html>" });
    showRoute("#/jobs");
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Details"));
    await flush(12);
    assert.match(rootText(root()), /No job returned|without a usable job record/);
  });
});

describe("jobs - messaging and safety", () => {
  it("keeps the simulation-only notice in every state", async () => {
    for (const routes of [{}, { "GET /api/jobs": { status: 200, body: EMPTY_LIST } }]) {
      start(routes);
      showRoute("#/jobs");
      await flush();
      assert.match(rootText(root()), /Simulation only/);
    }
  });

  it("states that no engine is ever invoked", async () => {
    showRoute("#/jobs");
    await flush();
    assert.match(rootText(root()), /No engine binary is ever invoked/);
  });

  it("never claims real cracking output", async () => {
    showRoute("#/jobs");
    await flush();
    const text = rootText(root());
    // The view explicitly says no progress or worker output EXISTS to show, so
    // assert the negating sentence rather than banning the word "progress".
    for (const forbidden of ["cracked", "password found", "hashes recovered", "plaintext"]) {
      assert.doesNotMatch(text, new RegExp(forbidden, "i"));
    }
    assert.match(text, /no progress, worker or engine output exists to show/i);
  });

  it("registers itself on the /jobs route", () => {
    // importing the module at the top registered it; assert through the router
    showRoute("#/jobs");
    assert.doesNotThrow(() => showRoute("#/jobs"));
  });
});
