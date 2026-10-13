import assert from "node:assert/strict";
import { describe, it, beforeEach } from "node:test";

import { showRoute } from "../frontend/js/app.js";
import "../frontend/js/views/plan.js";
import {
  FakeResponse,
  byTag,
  click,
  containing,
  deniedPlan,
  flush,
  rootText,
  selectValues,
  setup,
  type,
} from "./_fixtures.mjs";

const AUTHORIZED_PLAN = {
  target: "pdf-document",
  selection: {
    status: "selected",
    reason: "verified_capability",
    selected_engine: "john",
    candidates: ["john"],
  },
  request: {
    target: "pdf-document",
    engine: "mock_john",
    mode: "simulated",
    selection_status: "selected",
    capability_status: "verified",
    selected_engine: "john",
    matched_formats: ["pdf"],
    reference: "preview-1",
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
    steps: ["load_mock_engine", "simulate_compatibility_run", "collect_simulation_result"],
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
    steps: ["load_mock_engine", "simulate_compatibility_run", "collect_simulation_result"],
    message: "simulation completed",
    simulation_id: "abcdef0123456789",
    simulated: true,
  },
};

let env;
function start(plan) {
  env = setup({
    "GET /api/targets": {
      status: 200,
      body: {
        targets: [
          { target: "pdf-document", description: "PDF", formats: ["pdf"], enabled: true, usable: true },
          { target: "zip-archive", description: "Zip", formats: ["zip"], enabled: true, usable: false },
        ],
        count: 2,
      },
    },
    "POST /api/targets/pdf-document/compatibility/plan":
      typeof plan === "function" ? plan : { status: 200, body: plan },
  });
  return env;
}
function root() {
  return globalThis.document.getElementById("view-root");
}
function select(id) {
  return root().querySelectorAll(`[id="${id}"]`)[0];
}

beforeEach(() => {
  start(AUTHORIZED_PLAN);
});

describe("plan - target selection", () => {
  it("offers the real target types", async () => {
    showRoute("#/plan");
    await flush();
    assert.deepEqual(selectValues(select("plan-target")), ["", "pdf-document", "zip-archive"]);
  });

  it("only offers the simulation mode", async () => {
    showRoute("#/plan");
    await flush();
    assert.deepEqual(selectValues(select("plan-mode")), ["simulated"]);
  });

  it("sends exactly {mode, reference} in the body", async () => {
    showRoute("#/plan");
    await flush();
    const sel = select("plan-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    type(select("plan-reference"), "preview-1");
    click(root().querySelectorAll("button").find((b) => /Preview simulated plan/.test(b.textContent)));
    await flush(12);
    const posted = env.calls.find((c) => c.method === "POST");
    assert.ok(posted);
    assert.deepEqual(Object.keys(posted.body).sort(), ["mode", "reference"]);
    assert.equal(posted.body.mode, "simulated");
    assert.equal(posted.body.reference, "preview-1");
    assert.equal("target" in posted.body, false, "the target belongs in the path, not the body");
  });

  it("posts to the path carrying the selected target", async () => {
    showRoute("#/plan");
    await flush();
    const sel = select("plan-target");
    sel.value = "zip-archive";
    sel.dispatchEvent({ type: "change", target: sel });
    click(root().querySelectorAll("button").find((b) => /Preview simulated plan/.test(b.textContent)));
    await flush(12);
    const posted = env.calls.find((c) => c.method === "POST");
    assert.ok(posted);
    assert.equal(posted.url, "/api/targets/zip-archive/compatibility/plan");
  });
});

describe("plan - authorized response", () => {
  it("renders the authorized summary and the full preview", async () => {
    showRoute("#/plan?target=pdf-document");
    await flush();
    const text = rootText(root());
    assert.match(text, /Authorized - this is what a simulated run would look like/);
    assert.match(text, /No engine ran/);
  });

  it("renders the request block, including the reference", async () => {
    start(AUTHORIZED_PLAN);
    showRoute("#/plan?target=pdf-document");
    await flush();
    const text = rootText(root());
    assert.ok(text.includes("Request"), "the request section must render");
    assert.match(text, /preview-1/);
  });

  it("renders the plan steps and the simulation result", async () => {
    showRoute("#/plan?target=pdf-document");
    await flush();
    const text = rootText(root());
    assert.match(text, /load_mock_engine/);
    assert.match(text, /abcdef0123456789/);
    assert.match(text, /simulation completed/);
  });

  it("renders selection and authorization detail", async () => {
    showRoute("#/plan?target=pdf-document");
    await flush();
    const text = rootText(root());
    assert.match(text, /verified/);
    assert.match(text, /authorized_simulation/);
  });

  it("auto-submits when arriving with a target", async () => {
    showRoute("#/plan?target=pdf-document");
    await flush();
    assert.ok(
      env.calls.some((c) => c.url === "/api/targets/pdf-document/compatibility/plan"),
      "arriving with a target must request the preview automatically"
    );
  });
});

describe("plan - denied response (HTTP 200)", () => {
  it("shows a denial summary, not an error", async () => {
    start(deniedPlan());
    showRoute("#/plan?target=pdf-document");
    await flush();
    const text = rootText(root());
    assert.match(text, /Denied - no simulation was produced\./);
    assert.match(text, /capability_not_verified/);
    assert.doesNotMatch(text, /This section failed to load/);
  });

  it("still renders the authorization detail and no plan", async () => {
    start(deniedPlan());
    showRoute("#/plan?target=pdf-document");
    await flush();
    const text = rootText(root());
    assert.match(text, /none \(denied\)/);
    assert.match(text, /No simulation was produced for this request\./);
    assert.doesNotMatch(text, /load_mock_engine/);
  });

  it("distinguishes a non-simulation-mode denial", async () => {
    start(deniedPlan("non_simulation_request"));
    showRoute("#/plan?target=pdf-document");
    await flush();
    assert.match(rootText(root()), /non_simulation_request/);
  });

  it("branches on authorization.authorized, not on HTTP success", async () => {
    // both responses are HTTP 200; only the body differs
    start(AUTHORIZED_PLAN);
    showRoute("#/plan?target=pdf-document");
    await flush();
    assert.match(rootText(root()), /Authorized/);

    start(deniedPlan());
    showRoute("#/plan?target=pdf-document");
    await flush();
    assert.match(rootText(root()), /Denied/);
  });
});

describe("plan - states", () => {
  it("prompts for a target before any submission", async () => {
    showRoute("#/plan");
    await flush();
    assert.match(rootText(root()), /No preview yet/);
  });

  it("blocks submission with no target selected", async () => {
    showRoute("#/plan");
    await flush();
    click(root().querySelectorAll("button").find((b) => /Preview simulated plan/.test(b.textContent)));
    await flush();
    assert.match(rootText(root()), /Choose a target type first\./);
    assert.equal(env.calls.filter((c) => c.method === "POST").length, 0);
  });

  it("clears an arriving target the catalog does not contain", async () => {
    showRoute("#/plan?target=not-a-target");
    await flush();
    assert.equal(env.calls.filter((c) => c.method === "POST").length, 0);
    assert.match(rootText(root()), /No preview yet/);
  });

  it("shows an error and retry on an API failure", async () => {
    start(() => ({ status: 500, body: { detail: "boom" } }));
    showRoute("#/plan?target=pdf-document");
    await flush();
    const text = rootText(root());
    assert.match(text, /This section failed to load|Cannot reach/);
    assert.ok(root().querySelectorAll("button").some((b) => /Retry/.test(b.textContent)));
  });

  it("handles a non-JSON response body", async () => {
    start("<html>nope</html>");
    showRoute("#/plan?target=pdf-document");
    await flush();
    assert.match(rootText(root()), /The API answered without a usable preview body\./);
  });

  it("does not auto-submit when the catalog itself failed to load", async () => {
    // A broken fetch takes the catalog down too, so the arriving target is
    // unknown and the view must NOT fire a preview request.
    start(AUTHORIZED_PLAN);
    globalThis.fetch = async () => {
      throw new Error("offline");
    };
    showRoute("#/plan?target=pdf-document");
    await flush();
    assert.equal(
      env.calls.filter((c) => c.method === "POST").length,
      0,
      "an unknown target must not trigger a preview"
    );
    assert.match(rootText(root()), /No preview yet/);
  });

  it("shows the API error when only the preview request fails", async () => {
    start(AUTHORIZED_PLAN);
    globalThis.fetch = async (url) => {
      if (String(url).endsWith("/compatibility/plan")) {
        return new FakeResponse(500, { detail: "preview exploded" });
      }
      const { FakeResponse: FR } = await import("./_fixtures.mjs");
      return new FR(200, {
        targets: [
          { target: "pdf-document", description: "PDF", formats: ["pdf"], enabled: true, usable: true },
        ],
        count: 1,
      });
    };
    showRoute("#/plan?target=pdf-document");
    await flush();
    assert.match(rootText(root()), /preview exploded/);
  });
});

describe("plan - navigation and messaging", () => {
  it("links back to Targets", async () => {
    showRoute("#/plan");
    await flush();
    assert.ok(root().querySelectorAll("a").some((a) => a.getAttribute("href") === "#/targets"));
  });

  it("keeps the simulation-only notice", async () => {
    for (const plan of [AUTHORIZED_PLAN, deniedPlan()]) {
      start(plan);
      showRoute("#/plan?target=pdf-document");
      await flush();
      assert.match(rootText(root()), /Simulation only/);
    }
  });

  it("states that nothing is ever executed", async () => {
    showRoute("#/plan");
    await flush();
    assert.match(rootText(root()), /Nothing is ever executed/);
  });
});

void byTag;
void FakeResponse;
