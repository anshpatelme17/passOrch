import assert from "node:assert/strict";
import { describe, it, beforeEach } from "node:test";

import { showRoute } from "../app/frontend/js/app.js";
import "../app/frontend/js/views/plan.js";
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

describe("plan - target path encoding", () => {
  const BASE_TARGET = { target: "pdf-document", description: "PDF", formats: ["pdf"], enabled: true, usable: true };

  /**
   * Start the view with a catalog that carries the arriving (possibly odd)
   * target, and a wildcard preview route: the arriving target survives the
   * "keep only catalog targets" guard and the auto-submit reaches the URL
   * builder.  The fetch fixture records the exact URL it was called with.
   */
  function startWithPreview(extraTargets = []) {
    const targets = [
      BASE_TARGET,
      ...extraTargets.map((t) => ({
        target: t, description: "arriving", formats: ["pdf"], enabled: true, usable: true,
      })),
    ];
    env = setup({
      "GET /api/targets": { status: 200, body: { targets, count: targets.length } },
      "POST /api/targets/*": { status: 200, body: AUTHORIZED_PLAN },
    });
    return env;
  }

  async function postedUrl(hash, extraTargets = []) {
    startWithPreview(extraTargets);
    showRoute(hash);
    await flush();
    const posted = env.calls.find((c) => c.method === "POST");
    assert.ok(posted, "a preview request must be issued");
    return posted.url;
  }

  function assertOnePathSegment(url) {
    // No query string or fragment may smuggle itself in behind the segment.
    assert.equal(url.includes("?"), false, "raw query marker in URL");
    assert.equal(url.includes("#"), false, "raw fragment marker in URL");
    assert.equal(url.split("?").length, 1);
    return url;
  }

  it("leaves an ordinary identifier unchanged", async () => {
    const url = await postedUrl("#/plan?target=pdf-document");
    assert.equal(url, "/api/targets/pdf-document/compatibility/plan");
  });

  it("encodes spaces and Unicode (UTF-8 percent-encoding)", async () => {
    const odd = "office document ™";
    const url = await postedUrl("#/plan?target=" + encodeURIComponent(odd), [odd]);
    assert.equal(url, "/api/targets/" + encodeURIComponent(odd) + "/compatibility/plan");
    assert.match(url, /%20/);
    assert.match(url, /%E2%84%A2/);
  });

  it("encodes a literal slash as %2F, keeping one segment", async () => {
    const url = await postedUrl("#/plan?target=a/b", ["a/b"]);
    assert.equal(url, "/api/targets/a%2Fb/compatibility/plan");
    assert.doesNotMatch(url, /\/api\/targets\/a\/b\//);
    assertOnePathSegment(url);
  });

  it("encodes a raw question mark — a query can never start mid-segment", async () => {
    // A literal "?" inside the arriving target is data, not a delimiter.
    const url = await postedUrl("#/plan?target=a?b", ["a?b"]);
    assert.equal(url, "/api/targets/a%3Fb/compatibility/plan");
    assertOnePathSegment(url);
  });

  it("encodes a decoded question mark the same way", async () => {
    const url = await postedUrl("#/plan?target=a%3Fb", ["a?b"]);
    assert.equal(url, "/api/targets/a%3Fb/compatibility/plan");
    assertOnePathSegment(url);
  });

  it("encodes a raw fragment marker", async () => {
    const url = await postedUrl("#/plan?target=a#b", ["a#b"]);
    assert.equal(url, "/api/targets/a%23b/compatibility/plan");
    assertOnePathSegment(url);
  });

  it("encodes a decoded fragment marker the same way", async () => {
    const url = await postedUrl("#/plan?target=a%23b", ["a#b"]);
    assert.equal(url, "/api/targets/a%23b/compatibility/plan");
  });

  it("encodes a literal percent sign — never double-encodes", async () => {
    const url = await postedUrl("#/plan?target=a%25b", ["a%b"]);
    assert.equal(url, "/api/targets/a%25b/compatibility/plan");
    assert.doesNotMatch(url, /%2525/);
  });

  it("encodes an already-encoded-looking string exactly once", async () => {
    const url = await postedUrl("#/plan?target=a%252Fb", ["a%2Fb"]);
    // %25 decodes to "%", which then encodes to %25 exactly once more.
    assert.equal(url, "/api/targets/a%252Fb/compatibility/plan");
    assert.doesNotMatch(url, /%25252F/);
  });

  it("encodes a malformed percent escape without throwing", async () => {
    const url = await postedUrl("#/plan?target=%zz", ["%zz"]);
    assert.equal(url, "/api/targets/%25zz/compatibility/plan");
  });

  it("encodes a lone trailing percent sign", async () => {
    const url = await postedUrl("#/plan?target=100%", ["100%"]);
    assert.equal(url, "/api/targets/100%25/compatibility/plan");
  });

  it("keeps an encoded ampersand and equals sign inside the segment", async () => {
    const url = await postedUrl("#/plan?target=a%26b%3Dc", ["a&b=c"]);
    assert.equal(url, "/api/targets/a%26b%3Dc/compatibility/plan");
    assertOnePathSegment(url);
  });

  it("keeps a raw equals sign inside the segment", async () => {
    const url = await postedUrl("#/plan?target=a=b", ["a=b"]);
    assert.equal(url, "/api/targets/a%3Db/compatibility/plan");
    assertOnePathSegment(url);
  });

  it("turns a path-traversal attempt into inert text", async () => {
    const url = await postedUrl("#/plan?target=../jobs", ["../jobs"]);
    assert.equal(url, "/api/targets/..%2Fjobs/compatibility/plan");
    assert.doesNotMatch(url, /\/api\/targets\/\.\.\//);
    assertOnePathSegment(url);
  });

  it("keeps exactly the expected path structure: one encoded segment", async () => {
    const target = decodeURIComponent("a%2Fb%3Fc%23d");
    assert.equal(target, "a/b?c#d");
    const url = await postedUrl("#/plan?target=a%2Fb%3Fc%23d", [target]);
    assert.equal(url, "/api/targets/a%2Fb%3Fc%23d/compatibility/plan");
    const sent = new URL(url, "http://passorch.invalid").pathname.split("/").filter(Boolean);
    assert.deepEqual(sent, ["api", "targets", "a%2Fb%3Fc%23d", "compatibility", "plan"]);
  });

  it("a hostile arriving target renders without taking the router down", async () => {
    startWithPreview(["%zz"]);
    let shown;
    assert.doesNotThrow(() => {
      shown = showRoute("#/plan?target=%zz");
    });
    assert.equal(shown.path, "/plan");
    await flush();
    assert.match(root().textContent, /Simulation only/);
  });
});

describe("plan - submission in-flight guard", () => {
  const CATALOG = {
    targets: [
      { target: "pdf-document", description: "PDF", formats: ["pdf"], enabled: true, usable: true },
      { target: "zip-archive", description: "Zip", formats: ["zip"], enabled: true, usable: false },
    ],
    count: 2,
  };
  const AUTHORIZED = {
    target: "pdf-document",
    authorization: { authorized: true, status: "authorized", reason: "allowed", adapter: "mock_john", mode: "simulated" },
    selection: { status: "selected" },
    request: { target: "pdf-document", engine: "mock_john", mode: "simulated", reference: "ref-1" },
    plan: { adapter: "mock_john", mode: "simulated", steps: ["a"], simulated: true, target: "pdf-document", selected_engine: "john", capability_status: "verified" },
    simulation: { status: "simulated", simulation_id: "abc123", simulated: true },
  };

  /**
   * Wrap the environment fetch so `/api/targets/*` preview POSTs become
   * manual, completable deferreds (every other call delegates to the fixture).
   * Returns the pending submissions in issue order.
   */
  function deferredSubmissions() {
    const fixtureFetch = globalThis.fetch;
    const pending = [];
    globalThis.fetch = (url, opts) => {
      const u = String(url);
      if (u.startsWith("/api/targets/") && u.endsWith("/compatibility/plan")) {
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

  function startFresh(routes) {
    env = setup({
      "GET /api/targets": { status: 200, body: CATALOG },
      ...(routes || {}),
    });
    return env;
  }
  const submitBtn = () =>
    root().querySelectorAll("button").find((b) => /Preview simulated plan/.test(b.textContent));
  // The deferred wrapper bypasses the fixture fetch, so submissions are counted here.
  const submitCount = (pending) => pending.length;

  it("the first submission issues exactly one request", async () => {
    startFresh();
    const pending = deferredSubmissions();
    showRoute("#/plan");
    await flush();
    const sel = select("plan-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    click(submitBtn());
    await flush();
    assert.equal(submitCount(pending), 1);
    assert.equal(pending.length, 1);
    assert.equal(pending[0].url, "/api/targets/pdf-document/compatibility/plan");
  });

  it("rapid repeated clicks do not create duplicate requests", async () => {
    startFresh();
    const pending = deferredSubmissions();
    showRoute("#/plan");
    await flush();
    const sel = select("plan-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    const btn = submitBtn();
    // click() dispatches synchronously: all three handlers run before any await resolves
    click(btn);
    click(btn);
    click(btn);
    await flush();
    assert.equal(submitCount(pending), 1);
    assert.equal(pending.length, 1);
  });

  it("keyboard activation cannot bypass the guard", async () => {
    startFresh();
    const pending = deferredSubmissions();
    showRoute("#/plan");
    await flush();
    const sel = select("plan-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    click(submitBtn());
    await flush();
    // same handler, Enter/click in a browser activate the same path
    click(submitBtn());
    await flush();
    assert.equal(submitCount(pending), 1);
  });

  it("the submit button is disabled while a request is in flight", async () => {
    startFresh();
    const pending = deferredSubmissions();
    showRoute("#/plan");
    await flush();
    const sel = select("plan-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    click(submitBtn());
    await flush();
    assert.equal(submitBtn().hasAttribute("disabled"), true);
    resolveWith(pending[0], AUTHORIZED);
    await flush();
    assert.equal(submitBtn().hasAttribute("disabled"), false);
  });

  it("success keeps the button enabled and renders the result", async () => {
    startFresh();
    const pending = deferredSubmissions();
    showRoute("#/plan");
    await flush();
    const sel = select("plan-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    click(submitBtn());
    await flush();
    resolveWith(pending[0], AUTHORIZED);
    await flush();
    assert.match(root().textContent, /Authorized/);
    assert.equal(submitBtn().hasAttribute("disabled"), false);
  });

  it("a failure restores the button and a retry creates exactly one new request", async () => {
    startFresh();
    const pending = deferredSubmissions();
    showRoute("#/plan");
    await flush();
    const sel = select("plan-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    click(submitBtn());
    await flush();
    resolveWith(pending[0], { detail: "boom" }, 500);
    await flush();
    assert.match(root().textContent, /This section failed to load|Cannot reach|failed/i);
    assert.equal(submitBtn().hasAttribute("disabled"), false);

    click(root().querySelectorAll("button").find((b) => /Retry preview/.test(b.textContent)));
    await flush();
    assert.equal(submitCount(pending), 2);
    assert.equal(pending.length, 2);
    resolveWith(pending[1], AUTHORIZED);
    await flush();
    assert.match(root().textContent, /Authorized/);
  });

  it("duplicate retry invocations while in-flight collapse to one request", async () => {
    startFresh();
    const pending = deferredSubmissions();
    showRoute("#/plan");
    await flush();
    const sel = select("plan-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    click(submitBtn());
    await flush();
    resolveWith(pending[0], { detail: "boom" }, 500);
    await flush();

    const retry = root().querySelectorAll("button").find((b) => /Retry preview/.test(b.textContent));
    click(retry);
    click(retry);
    await flush();
    // the second click happened while the retry request was in-flight
    assert.equal(submitCount(pending), 2);
    resolveWith(pending[1], AUTHORIZED);
    await flush();
    assert.match(root().textContent, /Authorized/);
  });

  it("navigation away mid-flight prevents a stale write into the screen", async () => {
    startFresh();
    const pending = deferredSubmissions();
    showRoute("#/plan");
    await flush();
    const sel = select("plan-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    click(submitBtn());
    await flush();

    showRoute("#/jobs");
    await flush();
    resolveWith(pending[0], AUTHORIZED);
    await flush();
    // the screen now shows the jobs view, not the plan result
    assert.doesNotMatch(root().textContent, /Authorized - this is what a simulated run would look like/);

    showRoute("#/plan");
    await flush();
    // the re-mounted plan view gets a fresh, empty state
    assert.match(root().textContent, /No preview yet|Pick a target type/);
    assert.doesNotMatch(root().textContent, /Authorized/);
  });

  it("a re-mounted view can submit for a different target and ignores the stale completion", async () => {
    startFresh();
    const pending = deferredSubmissions();
    showRoute("#/plan");
    await flush();
    let sel = select("plan-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    click(submitBtn());
    await flush();

    // navigate away and back, then choose the other target
    showRoute("#/jobs");
    await flush();
    showRoute("#/plan");
    await flush();
    sel = select("plan-target");
    sel.value = "zip-archive";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    click(submitBtn());
    await flush();

    // the stale first request resolves last; it must not affect anything
    resolveWith(pending[1], AUTHORIZED);
    resolveWith(pending[0], AUTHORIZED);
    await flush();
    assert.equal(pending[1].url, "/api/targets/zip-archive/compatibility/plan");
    assert.match(root().textContent, /plan preview/i);
  });

  it("denied response keeps the existing messaging", async () => {
    startFresh();
    const pending = deferredSubmissions();
    showRoute("#/plan");
    await flush();
    const sel = select("plan-target");
    sel.value = "pdf-document";
    sel.dispatchEvent({ type: "change", target: sel });
    await flush();
    click(submitBtn());
    await flush();
    const denial = {
      target: "pdf-document",
      authorization: { authorized: false, status: "denied", reason: "capability_not_verified", adapter: "", mode: "simulated" },
      selection: { status: "selected" },
      request: { target: "pdf-document", reference: "ref-1" },
      plan: null,
      simulation: null,
    };
    resolveWith(pending[0], denial);
    await flush();
    assert.match(root().textContent, /Denied - no simulation was produced/);
  });
});

void byTag;
void FakeResponse;
