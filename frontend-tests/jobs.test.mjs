import assert from "node:assert/strict";
import { describe, it, beforeEach } from "node:test";

import { showRoute } from "../app/frontend/js/app.js";
import "../app/frontend/js/views/jobs.js";
import {
  FakeResponse,
  assertLoneShellBanner,
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
/** Choose a list filter through the real change handler. */
async function setFilter(id, value) {
  const sel = select(id);
  sel.value = value;
  sel.dispatchEvent({ type: "change", target: sel });
  await flush();
}
/** The per-row "Details" buttons, in list order. */
function detailButtons() {
  return root().querySelectorAll("button").filter((b) => b.textContent === "Details");
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

describe("jobs - detail selection synchronization", () => {
  it("clears the details when a filter removes the selected job", async () => {
    let listResponse = TWO_JOBS;
    start({
      "GET /api/jobs": () => ({ status: 200, body: listResponse }),
      [`GET /api/jobs/${FULL}`]: { status: 200, body: simulatedJob(null, FULL) },
    });
    showRoute("#/jobs");
    await flush();
    click(detailButtons()[0]);
    await flush(12);
    assert.match(rootText(root()), new RegExp(`Job ${FULL}`));

    listResponse = { jobs: [TWO_JOBS.jobs[1]], count: 1, total: 1 };
    await setFilter("jobs-filter-status", "denied");

    const text = rootText(root());
    assert.match(text, /No job selected/);
    assert.match(text, /Select a job from the current list to inspect its details\./);
    assert.doesNotMatch(text, new RegExp(`Job ${FULL}`));
  });

  it("a pending detail response cannot restore an excluded job", async () => {
    let listResponse = TWO_JOBS;
    start({
      "GET /api/jobs": () => ({ status: 200, body: listResponse }),
    });
    const fixtureFetch = env.fetch;
    let resolveDetail;
    const pendingDetail = new Promise((resolve) => {
      resolveDetail = resolve;
    });
    globalThis.fetch = (url, opts) => {
      if (String(url) === `/api/jobs/${FULL}`) {
        return pendingDetail;
      }
      return fixtureFetch(url, opts);
    };

    showRoute("#/jobs");
    await flush();
    click(detailButtons()[0]);
    await flush();
    assert.ok(resolveDetail, "the detail request must be issued");

    listResponse = EMPTY_LIST;
    await setFilter("jobs-filter-status", "denied");
    assert.match(rootText(root()), /No job selected/);

    resolveDetail(new FakeResponse(200, simulatedJob(null, FULL)));
    await flush(12);

    const text = rootText(root());
    assert.match(text, /No job selected/);
    assert.match(text, /Select a job from the current list to inspect its details\./);
    assert.doesNotMatch(text, new RegExp(`Job ${FULL}`));
  });

  it("reconciles the job actually painted, not just the selection marker", async () => {
    const otherJobId = "77777777-7777-4777-8777-777777777777";
    start({
      "GET /api/jobs": () => ({ status: 200, body: TWO_JOBS }),
      // Simulate a detail panel whose painted job differs from the selection.
      [`GET /api/jobs/${FULL}`]: { status: 200, body: simulatedJob(null, otherJobId) },
    });
    showRoute("#/jobs");
    await flush();
    click(detailButtons()[0]);
    await flush(12);
    assert.match(rootText(root()), new RegExp(`Job ${otherJobId}`));

    // The list still contains FULL, so the old selection-only check would keep
    // the panel; the painted job itself is not visible on this page.
    await setFilter("jobs-filter-status", "simulated");

    const text = rootText(root());
    assert.match(text, new RegExp(FULL));
    assert.match(text, /No job selected/);
    assert.doesNotMatch(text, new RegExp(`Job ${otherJobId}`));
  });

  it("retains the details when the selected job survives the filter", async () => {
    let listResponse = TWO_JOBS;
    start({
      "GET /api/jobs": () => ({ status: 200, body: listResponse }),
      [`GET /api/jobs/${FULL}`]: { status: 200, body: simulatedJob(null, FULL) },
    });
    showRoute("#/jobs");
    await flush();
    click(detailButtons()[0]);
    await flush(12);
    assert.match(rootText(root()), new RegExp(`Job ${FULL}`));

    listResponse = { jobs: [TWO_JOBS.jobs[0]], count: 1, total: 1 };
    await setFilter("jobs-filter-status", "simulated");

    const text = rootText(root());
    assert.match(text, new RegExp(`Job ${FULL}`));
    assert.match(text, /Simulated outcome recorded/);
    assert.doesNotMatch(text, /No job selected/);
  });

  it("shows the filtered empty list without stale details", async () => {
    let listResponse = TWO_JOBS;
    start({
      "GET /api/jobs": () => ({ status: 200, body: listResponse }),
      [`GET /api/jobs/${FULL}`]: { status: 200, body: simulatedJob(null, FULL) },
    });
    showRoute("#/jobs");
    await flush();
    click(detailButtons()[0]);
    await flush(12);
    assert.match(rootText(root()), new RegExp(`Job ${FULL}`));

    listResponse = EMPTY_LIST;
    await setFilter("jobs-filter-status", "denied");

    const text = rootText(root());
    assert.match(text, /No jobs match your filters/);
    assert.match(text, /Adjust or clear the filters above to see recorded jobs again\./);
    assert.match(text, /No job selected/);
    assert.match(text, /Select a job from the current list to inspect its details\./);
    assert.doesNotMatch(text, new RegExp(`Job ${FULL}`));
    assert.doesNotMatch(text, /No jobs recorded yet/);
  });

  it("does not restore a cleared selection when the filters are cleared", async () => {
    let listResponse = TWO_JOBS;
    start({
      "GET /api/jobs": () => ({ status: 200, body: listResponse }),
      [`GET /api/jobs/${FULL}`]: { status: 200, body: simulatedJob(null, FULL) },
    });
    showRoute("#/jobs");
    await flush();
    click(detailButtons()[0]);
    await flush(12);
    assert.match(rootText(root()), new RegExp(`Job ${FULL}`));

    listResponse = { jobs: [TWO_JOBS.jobs[1]], count: 1, total: 1 };
    await setFilter("jobs-filter-status", "denied");
    assert.match(rootText(root()), /No job selected/);

    listResponse = TWO_JOBS;
    await setFilter("jobs-filter-status", "");

    const text = rootText(root());
    assert.match(text, new RegExp(FULL), "the list must show the job again");
    assert.match(text, /No job selected/);
    assert.doesNotMatch(text, new RegExp(`Job ${FULL}`));
  });

  it("loads the details of another visible job", async () => {
    start({
      [`GET /api/jobs/${FULL}`]: { status: 200, body: simulatedJob(null, FULL) },
      "GET /api/jobs/22222222-2222-4222-8222-222222222222": {
        status: 200,
        body: { ...deniedJob(), job_id: "22222222-2222-4222-8222-222222222222" },
      },
    });
    showRoute("#/jobs");
    await flush();
    click(detailButtons()[0]);
    await flush(12);
    assert.match(rootText(root()), new RegExp(`Job ${FULL}`));
    assert.match(rootText(root()), /Simulated outcome recorded/);

    click(detailButtons()[1]);
    await flush(12);

    const text = rootText(root());
    assert.match(text, /Job 22222222-2222-4222-8222-222222222222/);
    assert.match(text, /This job was denied by the execution policy/);
    assert.doesNotMatch(text, new RegExp(`Job ${FULL}`));
  });

  it("scopes the details to the page that is showing", async () => {
    const pageTwoJobId = "99999999-9999-4999-8999-999999999999";
    const pageOne = {
      jobs: [
        TWO_JOBS.jobs[0],
        ...Array.from({ length: 49 }, (_, i) => ({
          job_id: `aaaaaaaa-aaaa-4aaa-8aaa-${String(i).padStart(12, "0")}`,
          status: "simulated",
          created_at: 1700000000 - i,
          target: "pdf-document",
        })),
      ],
      count: 51,
      total: 51,
    };
    const pageTwo = {
      jobs: [
        {
          job_id: pageTwoJobId,
          status: "simulated",
          created_at: 1700000000,
          target: "pdf-document",
        },
      ],
      count: 1,
      total: 51,
    };
    start({
      "GET /api/jobs": () => {
        const call = env.calls
          .filter((c) => c.method === "GET" && String(c.url).split("?")[0] === "/api/jobs")
          .pop();
        const offset = Number(
          new URL(call.url, "http://passorch.invalid").searchParams.get("offset") || "0"
        );
        return { status: 200, body: offset >= 50 ? pageTwo : pageOne };
      },
      [`GET /api/jobs/${FULL}`]: { status: 200, body: simulatedJob(null, FULL) },
      [`GET /api/jobs/${pageTwoJobId}`]: {
        status: 200,
        body: simulatedJob(null, pageTwoJobId),
      },
    });
    showRoute("#/jobs");
    await flush();
    click(detailButtons()[0]);
    await flush(12);
    assert.match(rootText(root()), new RegExp(`Job ${FULL}`));

    click(root().querySelectorAll("button").find((b) => b.textContent === "Next"));
    await flush();

    let text = rootText(root());
    assert.match(text, new RegExp(pageTwoJobId), "the second page must render");
    assert.match(text, /No job selected/);
    assert.doesNotMatch(text, new RegExp(`Job ${FULL}`));

    click(detailButtons()[0]);
    await flush(12);
    text = rootText(root());
    assert.match(text, new RegExp(`Job ${pageTwoJobId}`));
    assert.doesNotMatch(text, /No job selected/);
  });

  it("clears the details when the selected job is deleted", async () => {
    let listResponse = TWO_JOBS;
    start({
      "GET /api/jobs": () => ({ status: 200, body: listResponse }),
      [`GET /api/jobs/${FULL}`]: { status: 200, body: simulatedJob(null, FULL) },
      "DELETE /api/jobs/*": () => {
        listResponse = { jobs: [TWO_JOBS.jobs[1]], count: 1, total: 1 };
        return { status: 204, body: undefined };
      },
    });
    showRoute("#/jobs");
    await flush();
    click(detailButtons()[0]);
    await flush(12);
    assert.match(rootText(root()), new RegExp(`Job ${FULL}`));

    click(root().querySelectorAll("button").find((b) => b.textContent === "Delete"));
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Yes, delete"));
    await flush(14);

    const text = rootText(root());
    assert.match(text, /No job selected/);
    assert.match(text, /Select a job from the current list to inspect its details\./);
    assert.doesNotMatch(text, new RegExp(`Job ${FULL}`));
    assert.match(text, /22222222-2222-4222-8222-222222222222/);
  });
});

describe("jobs - export", () => {
  const EXPORT_PATH = `GET /api/jobs/${FULL}/export`;
  const ENVELOPE = {
    schema_version: "1",
    exported_at: 1793150000,
    simulation_only: true,
    job: simulatedJob(null, FULL),
  };

  /** Route one job's detail plus its M2.1 export envelope. */
  function withDetailAndExport(extra) {
    env = setup({
      "GET /api/targets": { status: 200, body: CATALOG },
      "GET /api/target-instances": { status: 200, body: INSTANCES },
      "GET /api/jobs": { status: 200, body: TWO_JOBS },
      "GET /api/jobs/11111111-1111-4111-8111-111111111111": {
        status: 200,
        body: simulatedJob(null, FULL),
      },
      [EXPORT_PATH]: { status: 200, body: ENVELOPE },
      ...(extra || {}),
    });
    return env;
  }

  /** Open the first job's detail panel through the real list button. */
  async function openDetail() {
    showRoute("#/jobs");
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Details"));
    await flush(12);
  }

  /** Spy on object-URL creation/revocation around `fn`. */
  async function withDownloadSpy(fn) {
    const created = [];
    const revoked = [];
    const originalCreate = URL.createObjectURL;
    const originalRevoke = URL.revokeObjectURL;
    URL.createObjectURL = (blob) => {
      const url = originalCreate.call(URL, blob);
      created.push({ blob, url });
      return url;
    };
    URL.revokeObjectURL = (url) => {
      revoked.push(url);
      originalRevoke.call(URL, url);
    };
    try {
      await fn({ created, revoked });
    } finally {
      URL.createObjectURL = originalCreate;
      URL.revokeObjectURL = originalRevoke;
    }
  }

  it("offers Export JSON in the opened detail but never fetches it unasked", async () => {
    withDetailAndExport();
    await openDetail();
    assert.ok(btn("Export JSON"), "the detail panel must offer the export");
    assert.equal(env.calls.filter((c) => c.url.includes("/export")).length, 0);
  });

  it("requests the export endpoint on click and downloads the exact envelope", async () => {
    withDetailAndExport();
    await openDetail();
    await withDownloadSpy(async ({ created, revoked }) => {
      click(btn("Export JSON"));
      await flush();
      const exportCalls = env.calls.filter((c) => c.url.includes("/export"));
      assert.equal(exportCalls.length, 1);
      assert.equal(exportCalls[0].method, "GET");
      assert.equal(exportCalls[0].url, `/api/jobs/${FULL}/export`);
      assert.equal(created.length, 1, "one blob must be produced");
      assert.equal(created[0].blob.type, "application/json");
      assert.equal(await created[0].blob.text(), JSON.stringify(ENVELOPE));
      assert.equal(revoked.length, 1, "the object URL must be revoked");
      assert.equal(revoked[0], created[0].url);
      assert.match(rootText(root()), /Export downloaded as a JSON file/);
      // the job record is still on the page - the export replaced nothing
      assert.match(rootText(root()), /Job 11111111-1111-4111-8111-111111111111/);
    });
  });

  it("names the file after the job id, never the operator reference", async () => {
    const job = simulatedJob(null, FULL);
    job.preview.request.reference = 'evil"; filename="pwn';
    withDetailAndExport({
      [EXPORT_PATH]: { status: 200, body: { ...ENVELOPE, job } },
    });
    await openDetail();
    // give the download a real body so the temporary anchor is mounted
    const body = env.document.createElement("body");
    globalThis.document.body = body;
    const mounted = [];
    const originalAppend = body.appendChild.bind(body);
    body.appendChild = (node) => {
      mounted.push(node);
      return originalAppend(node);
    };
    try {
      click(btn("Export JSON"));
      await flush();
      assert.equal(mounted.length, 1);
      const anchor = mounted[0];
      assert.equal(anchor.tagName, "A");
      assert.equal(anchor.getAttribute("download"), `passorch-job-${FULL}.json`);
      assert.match(anchor.getAttribute("href"), /^blob:/);
      assert.equal(anchor.getAttribute("download").includes("evil"), false);
      // the temporary anchor is removed again
      assert.equal(body.childNodes.length, 0);
    } finally {
      delete globalThis.document.body;
    }
  });

  it("reports a 404 export as a failure and downloads nothing", async () => {
    withDetailAndExport({
      [EXPORT_PATH]: {
        status: 404,
        body: { detail: { error: "job_not_found", detail: "job does not exist" } },
      },
    });
    await openDetail();
    await withDownloadSpy(async ({ created }) => {
      click(btn("Export JSON"));
      await flush();
      assert.equal(created.length, 0);
      assert.match(rootText(root()), /job_not_found/);
      assert.match(rootText(root()), /The export was not generated\./);
    });
  });

  it("treats a transport failure as a failed export", async () => {
    withDetailAndExport();
    const delegate = globalThis.fetch;
    globalThis.fetch = (url, options) => {
      if (String(url).includes("/export")) {
        return Promise.reject(new Error("offline"));
      }
      return delegate(url, options);
    };
    await openDetail();
    await withDownloadSpy(async ({ created }) => {
      click(btn("Export JSON"));
      await flush(12);
      assert.equal(created.length, 0);
      assert.match(rootText(root()), /The export was not generated\./);
    });
  });

  it("refuses to download a 200 whose body is not JSON", async () => {
    withDetailAndExport({
      // raw-text spec: an implied 200 whose body cannot be parsed
      [EXPORT_PATH]: "<html>not the envelope</html>",
    });
    await openDetail();
    await withDownloadSpy(async ({ created }) => {
      click(btn("Export JSON"));
      await flush();
      assert.equal(created.length, 0, "an unparseable body must never become a file");
      assert.match(rootText(root()), /The export was not generated\./);
    });
  });

  it("ignores a second click while the first export is in flight", async () => {
    withDetailAndExport();
    await openDetail();
    click(btn("Export JSON"));
    click(btn("Export JSON")); // same tick: the guard must swallow it
    await flush();
    assert.equal(env.calls.filter((c) => c.url.includes("/export")).length, 1);
  });

  it("reports a download failure with a fixed sentence, not the exception", async () => {
    withDetailAndExport();
    await openDetail();
    const originalCreate = URL.createObjectURL;
    URL.createObjectURL = () => {
      throw new Error("object URLs are unavailable in this realm");
    };
    try {
      click(btn("Export JSON"));
      await flush();
    } finally {
      URL.createObjectURL = originalCreate;
    }
    const text = rootText(root());
    assert.match(text, /The export was not generated\./);
    assert.match(text, /did not complete|try again/i);
    assert.doesNotMatch(text, /object URLs are unavailable/, "exception text must never reach the DOM");
    assert.doesNotMatch(text, /Error:/, "no raw error prefix in the DOM");
  });

  it("allows a fresh export after a failed one", async () => {
    let fail = true;
    withDetailAndExport({
      [EXPORT_PATH]: () =>
        fail
          ? { status: 404, body: { detail: { error: "job_not_found" } } }
          : { status: 200, body: ENVELOPE },
    });
    await openDetail();
    click(btn("Export JSON"));
    await flush();
    assert.match(rootText(root()), /The export was not generated\./);
    fail = false;
    await withDownloadSpy(async ({ created }) => {
      click(btn("Export JSON"));
      await flush();
      assert.equal(created.length, 1);
      assert.match(rootText(root()), /Export downloaded as a JSON file/);
    });
  });
});

describe("jobs - messaging and safety", () => {
  it("leaves the shell's single simulation banner untouched in every state", async () => {
    for (const routes of [{}, { "GET /api/jobs": { status: 200, body: EMPTY_LIST } }]) {
      start(routes);
      showRoute("#/jobs");
      await flush();
      const banner = assertLoneShellBanner(globalThis.document);
      assert.match(banner.textContent, /Simulation only/);
      assert.doesNotMatch(
        rootText(root()),
        /Simulation only/,
        "the Jobs view must not duplicate the shell banner"
      );
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

describe("jobs - in-flight guards reset on re-entry", () => {
  it("a submit left pending across navigation does not latch the form", async () => {
    start();
    const fixtureFetch = env.fetch;
    let posts = 0;
    globalThis.fetch = (url, opts) => {
      const method = String((opts && opts.method) || "GET").toUpperCase();
      if (method === "POST" && String(url).startsWith("/api/jobs")) {
        posts += 1;
        if (posts === 1) {
          return new Promise(() => {}); // hangs: its `finally` never runs
        }
      }
      return fixtureFetch(url, opts);
    };

    showRoute("#/jobs");
    await flush();
    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush();
    assert.equal(posts, 1, "the first submit must be issued");

    // Leave while that request is still in flight, then come back.
    showRoute("#/dashboard");
    await flush();
    showRoute("#/jobs");
    await flush();

    await choose("pdf-document");
    click(btn("Create simulated job"));
    await flush();
    assert.equal(posts, 2, "re-entry must reset the latched submit guard");
  });

  it("a delete left pending across navigation does not latch the list", async () => {
    start({
      "DELETE /api/jobs/*": { status: 204, body: undefined },
    });
    const fixtureFetch = env.fetch;
    let deletes = 0;
    globalThis.fetch = (url, opts) => {
      const method = String((opts && opts.method) || "GET").toUpperCase();
      if (method === "DELETE" && String(url).startsWith("/api/jobs/")) {
        deletes += 1;
        if (deletes === 1) {
          return new Promise(() => {});
        }
      }
      return fixtureFetch(url, opts);
    };

    showRoute("#/jobs");
    await flush();
    const row = containing(root(), FULL);
    assert.ok(row, "the job row must render");
    const del = row.querySelectorAll("button").find((b) => b.textContent === "Delete");
    assert.ok(del, "the row must offer Delete");
    click(del);
    await flush();
    const yes = root().querySelectorAll("button").find((b) => b.textContent === "Yes, delete");
    assert.ok(yes, "confirmation must be offered");
    click(yes);
    await flush();
    assert.equal(deletes, 1, "the first delete must be issued");

    showRoute("#/dashboard");
    await flush();
    showRoute("#/jobs");
    await flush();

    const row2 = containing(root(), FULL);
    const del2 = row2.querySelectorAll("button").find((b) => b.textContent === "Delete");
    click(del2);
    await flush();
    const yes2 = root().querySelectorAll("button").find((b) => b.textContent === "Yes, delete");
    click(yes2);
    await flush();
    assert.equal(deletes, 2, "re-entry must reset the latched delete guard");
  });
});

describe("jobs - list request sequencing", () => {
  const FULL_PAGE = {
    jobs: [
      { job_id: FULL, status: "simulated", created_at: 1700000001, target: "pdf-document" },
      { job_id: "22222222-2222-4222-8222-222222222222", status: "denied", created_at: 1700000002, target: "zip-archive" },
    ],
    count: 2,
    total: 120,
    limit: 50,
    offset: 0,
  };
  const FILTERED_PAGE = {
    jobs: [
      { job_id: "66666666-6666-4666-8666-666666666666", status: "simulated", created_at: 1700000009, target: "pdf-document" },
    ],
    count: 1,
    total: 1,
    limit: 50,
    offset: 0,
  };
  const MARKER = "66666666-6666-4666-8666-666666666666";

  /**
   * Wrap the environment fetch so starting `POST`s behave as usual but
   * `/api/jobs*` requests become manual, completable deferreds. Everything
   * else delegates to the fixture fetch. Returns the pending list requests
   * in issue order; `resolveWith(d, body, status?)` completes one.
   */
  function deferredListRequests() {
    const fixtureFetch = globalThis.fetch;
    const pending = [];
    globalThis.fetch = (url, opts) => {
      const u = String(url);
      if (u === "/api/jobs" || u.startsWith("/api/jobs?")) {
        const d = { url: u };
        d.promise = new Promise((resolve, reject) => {
          d.resolve = resolve;
          d.reject = reject;
        });
        pending.push(d);
        return d.promise;
      }
      return fixtureFetch(url, opts);
    };
    return pending;
  }
  const resolveWith = (d, body, status = 200) => d.resolve(new FakeResponse(status, body));
  const lastPending = (pending) => pending[pending.length - 1];

  it("an older success never overwrites a newer success (reverse order)", async () => {
    const pending = deferredListRequests();

    showRoute("#/jobs");
    await flush();
    assert.equal(pending.length, 1);

    // issue the newer request by narrowing the filter
    const sel = select("jobs-filter-status");
    sel.value = "simulated";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    assert.equal(pending.length, 2);
    assert.match(pending[1].url, /status=simulated/);

    // resolve the newer request first, then the older one
    resolveWith(pending[1], FILTERED_PAGE);
    await flush();
    assert.match(root().textContent, new RegExp(MARKER));

    resolveWith(pending[0], FULL_PAGE);
    await flush();
    const text = root().textContent;
    assert.match(text, new RegExp(MARKER), "the newer page must stay on screen");
    assert.doesNotMatch(text, /11111111-1111-4111-8111-111111111111/, "stale page must not repaint");
  });

  it("an older success never overwrites a newer failure", async () => {
    const pending = deferredListRequests();

    showRoute("#/jobs");
    await flush();
    const sel = select("jobs-filter-status");
    sel.value = "simulated";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    assert.equal(pending.length, 2);

    resolveWith(pending[1], { detail: "boom" }, 500);
    await flush();
    assert.match(root().textContent, /This section failed to load|Cannot reach/);
    resolveWith(pending[0], FULL_PAGE);
    await flush();
    assert.doesNotMatch(root().textContent, /11111111-1111-4111-8111-111111111111/);
    assert.match(root().textContent, /This section failed to load|Cannot reach/);
  });

  it("an older failure never overwrites a newer success", async () => {
    const pending = deferredListRequests();

    showRoute("#/jobs");
    await flush();
    const sel = select("jobs-filter-status");
    sel.value = "simulated";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();

    resolveWith(pending[1], FILTERED_PAGE);
    await flush();
    assert.match(root().textContent, new RegExp(MARKER));

    resolveWith(pending[0], { detail: "boom" }, 500);
    await flush();
    assert.match(root().textContent, new RegExp(MARKER));
    assert.doesNotMatch(root().textContent, /This section failed to load|Cannot reach/);
  });

  it("loading state stays owned by the newest request only", async () => {
    const pending = deferredListRequests();

    showRoute("#/jobs");
    await flush();
    const sel = select("jobs-filter-status");
    sel.value = "simulated";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    assert.match(root().textContent, /Loading jobs\.\.\./);

    // resolving the superseded request must not clear the pending state
    resolveWith(pending[0], FULL_PAGE);
    await flush();
    assert.match(root().textContent, /Loading jobs\.\.\./, "the newer request owns the loading state");

    resolveWith(pending[1], FILTERED_PAGE);
    await flush();
    assert.doesNotMatch(root().textContent, /Loading jobs\.\.\./);
    assert.match(root().textContent, new RegExp(MARKER));
  });

  it("a filter change made while a request is in flight supersedes it", async () => {
    const pending = deferredListRequests();

    showRoute("#/jobs");
    await flush();
    const instanceSel = select("jobs-filter-instance");
    instanceSel.value = INSTANCE;
    instanceSel.dispatchEvent({ type: "change", target: instanceSel });
    await flush();
    assert.equal(pending.length, 2);
    assert.match(pending[1].url, /instance_id=33333333-3333-4333-8333-333333333333/);

    resolveWith(pending[1], FILTERED_PAGE);
    await flush();
    resolveWith(pending[0], FULL_PAGE);
    await flush();
    assert.match(root().textContent, new RegExp(MARKER));
    assert.doesNotMatch(root().textContent, /11111111-1111-4111-8111-111111111111/);
  });

  it("an in-flight response after navigating away cannot repaint the screen", async () => {
    const pending = deferredListRequests();

    showRoute("#/jobs");
    await flush();
    assert.equal(pending.length, 1);

    showRoute("#/targets");
    await flush();
    // the targets screen is up; the list response arrives late
    resolveWith(pending[0], FULL_PAGE);
    await flush();
    assert.doesNotMatch(root().textContent, /11111111-1111-4111-8111-111111111111/);

    // and navigating back issues a fresh request on a fresh slot
    showRoute("#/jobs");
    await flush();
    assert.equal(pending.length, 2);
    resolveWith(pending[1], FILTERED_PAGE);
    await flush();
    assert.match(root().textContent, new RegExp(MARKER));
  });

  it("pagination requests supersede and keep only the latest page on screen", async () => {
    const pending = deferredListRequests();

    showRoute("#/jobs");
    await flush();
    resolveWith(pending[0], FULL_PAGE);
    await flush();
    assert.match(root().textContent, /Page 1 of 3 \(120 total\)/);

    click(btn("Next"));
    await flush();
    assert.equal(pending.length, 2);
    assert.match(pending[1].url, /offset=50/);

    // resolve the newer page, then the obsolete whole-page issue
    resolveWith(pending[1], { ...FILTERED_PAGE, limit: 50, offset: 50 });
    await flush();
    assert.match(root().textContent, new RegExp(MARKER));
    assert.doesNotMatch(root().textContent, /Page 1 of 3/);
  });

  it("an out-of-range page resubmits offset 0 against the newest issue only", async () => {
    const pending = deferredListRequests();

    showRoute("#/jobs");
    await flush();
    resolveWith(pending[0], FULL_PAGE);
    await flush();

    click(btn("Next"));
    await flush();
    assert.equal(pending.length, 2);

    // newer page answers empty while on offset 50: resubmits offset 0
    resolveWith(pending[1], { jobs: [], count: 0, total: 120, limit: 50, offset: 50 });
    await flush();
    assert.equal(pending.length, 3);
    assert.match(pending[2].url, /offset=0/);

    // and the resubmission renders normally
    resolveWith(pending[2], FULL_PAGE);
    await flush();
    assert.match(root().textContent, /11111111-1111-4111-8111-111111111111/);
  });

  it("initial mount shows a loading state and a failure renders a retry path", async () => {
    const pending = deferredListRequests();

    showRoute("#/jobs");
    await flush();
    assert.match(root().textContent, /Loading jobs\.\.\./);

    resolveWith(pending[0], { detail: "boom" }, 500);
    await flush();
    assert.match(root().textContent, /This section failed to load|Cannot reach/);

    click(btn("Retry"));
    await flush();
    assert.equal(pending.length, 2);
    assert.match(pending[1].url, /offset=0/);

    resolveWith(pending[1], FULL_PAGE);
    await flush();
    assert.match(root().textContent, /11111111-1111-4111-8111-111111111111/);
  });

  it("an older empty response cannot blank the newer view", async () => {
    const pending = deferredListRequests();

    showRoute("#/jobs");
    await flush();
    const sel = select("jobs-filter-status");
    sel.value = "simulated";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    assert.equal(pending.length, 2);

    // newer issue resolves with data; the old in-flight issue answers empty
    resolveWith(pending[1], FILTERED_PAGE);
    await flush();
    assert.match(root().textContent, new RegExp(MARKER));

    resolveWith(pending[0], { jobs: [], count: 0, total: 0, limit: 50, offset: 0 });
    await flush();
    const text = root().textContent;
    assert.match(text, new RegExp(MARKER));
    assert.doesNotMatch(text, /No jobs recorded yet/);
    assert.doesNotMatch(text, /No jobs match your filters/);
  });
});
