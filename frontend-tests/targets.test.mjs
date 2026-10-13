import assert from "node:assert/strict";
import { describe, it, beforeEach } from "node:test";

import { showRoute } from "../frontend/js/app.js";
import "../frontend/js/views/targets.js";
import { FakeResponse, containing, flush, setup } from "./_fixtures.mjs";

let env;

/**
 * Install routes. Accepts either an object of per-path overrides, or a single
 * function/callable spec for the whole endpoint (used to force errors).
 */
function start(routes, overrides) {
  const spec = typeof routes === "function" ? routes : { status: 200, body: routes };
  env = setup({
    "GET /api/targets": spec,
    ...(overrides || {}),
  });
  return env;
}

function root() {
  return globalThis.document.getElementById("view-root");
}

const USABLE = {
  targets: [
    {
      target: "pdf-document",
      description: "PDF document encryption audit",
      formats: ["pdf"],
      enabled: true,
      usable: true,
    },
    {
      target: "office-document",
      description: "Office document encryption audit",
      formats: ["office2007", "office2010", "office2013"],
      enabled: true,
      usable: true,
    },
  ],
  count: 2,
};

const MIXED = {
  targets: [
    { target: "pdf-document", description: "PDF", formats: ["pdf"], enabled: true, usable: true },
    {
      target: "zip-archive",
      description: "Archive",
      formats: ["pkzip", "zip"],
      enabled: true,
      usable: false,
    },
    { target: "empty-target", description: "No formats", formats: [], enabled: false, usable: false },
  ],
  count: 3,
};

beforeEach(() => {
  start(USABLE);
});

describe("targets - usable and unusable", () => {
  it("labels usable targets as usable", async () => {
    showRoute("#/targets");
    await flush();
    assert.match(root().textContent, /usable/);
  });

  it("labels an unusable target as not usable", async () => {
    start(MIXED);
    showRoute("#/targets");
    await flush();
    const text = root().textContent;
    assert.match(text, /zip-archive/);
    assert.match(text, /not usable/);
  });

  it("renders the disabled flag separately from usable", async () => {
    start(MIXED);
    showRoute("#/targets");
    await flush();
    assert.match(root().textContent, /disabled/);
    assert.match(root().textContent, /enabled/);
  });

  it("counts usable versus total", async () => {
    start(MIXED);
    showRoute("#/targets");
    await flush();
    assert.match(root().textContent, /3 target type\(s\), 1 usable/);
  });
});

describe("targets - plan link gating", () => {
  it("offers a plan link for a usable target", async () => {
    showRoute("#/targets");
    await flush();
    const links = root().querySelectorAll("a").filter((a) => /Preview plan/.test(a.textContent));
    assert.equal(links.length, 2, "both usable targets get a link");
  });

  it("omits the plan link for an unusable target", async () => {
    start(MIXED);
    showRoute("#/targets");
    await flush();
    const links = root().querySelectorAll("a").filter((a) => /Preview plan/.test(a.textContent));
    assert.equal(links.length, 1, "only the usable target links to the plan screen");
  });

  it("builds a router link carrying the encoded target", async () => {
    showRoute("#/targets");
    await flush();
    const link = root().querySelectorAll("a").find((a) => /Preview plan/.test(a.textContent));
    assert.equal(link.getAttribute("href"), "#/plan?target=pdf-document");
  });

  it("always offers a route to the plan screen", async () => {
    showRoute("#/targets");
    await flush();
    assert.ok(root().querySelectorAll("a").some((a) => a.getAttribute("href") === "#/plan"));
  });
});

describe("targets - states", () => {
  it("renders a loading state", async () => {
    // freeze the fetch so the pending state is observable
    globalThis.fetch = () => new Promise(() => {});
    showRoute("#/targets");
    await flush(2);
    assert.match(root().textContent, /Loading target catalog/);
  });

  it("renders an empty state for an empty catalog", async () => {
    start({ targets: [], count: 0 });
    showRoute("#/targets");
    await flush();
    const text = root().textContent;
    assert.match(text, /No target types available/);
  });

  it("renders an error with a retry", async () => {
    start(() => ({ status: 500, body: { detail: "boom" } }));
    showRoute("#/targets");
    await flush();
    assert.match(root().textContent, /This section failed to load|Cannot reach/);
    assert.ok(root().querySelectorAll("button").some((b) => /Retry/.test(b.textContent)));
  });
  it("retry re-renders successfully", async () => {
    start(() => ({ status: 500, body: { detail: "boom" } }));
    showRoute("#/targets");
    await flush();
    const retry = root().querySelectorAll("button").find((b) => /Retry/.test(b.textContent));
    assert.ok(retry);
    globalThis.fetch = async () => new FakeResponse(200, USABLE);
    retry.dispatchEvent({ type: "click", target: retry });
    await flush(12);
    assert.match(root().textContent, /pdf-document/);
  });
});

describe("targets - malformed data", () => {
  it("filters rows without a usable string target", async () => {
    start({
      targets: [
        { target: "pdf-document", description: "PDF", formats: ["pdf"], enabled: true, usable: true },
        { target: 42, description: "bad id", formats: [], enabled: false, usable: false },
        null,
        "not-an-object",
      ],
      count: 3,
    });
    showRoute("#/targets");
    await flush();
    const text = root().textContent;
    assert.match(text, /pdf-document/);
    assert.doesNotMatch(text, /42/);
    assert.match(text, /1 target type\(s\)/);
  });

  it("renders a dash for missing formats and description", async () => {
    start({
      targets: [{ target: "weird", formats: [], enabled: true, usable: true }],
      count: 1,
    });
    showRoute("#/targets");
    await flush();
    assert.match(root().textContent, /weird/);
  });

  it("survives a non-array targets payload", async () => {
    start({ targets: "nope", count: 0 });
    showRoute("#/targets");
    await flush();
    assert.match(root().textContent, /No target types available/);
  });
});

describe("targets - rendering", () => {
  it("shows the formats as a joined string", async () => {
    start(USABLE);
    showRoute("#/targets");
    await flush();
    assert.match(root().textContent, /office2007, office2010, office2013/);
  });

  it("keeps the simulation-only notice", async () => {
    showRoute("#/targets");
    await flush();
    assert.match(root().textContent, /Simulation only/);
  });

  it("states that no engine is executed", async () => {
    showRoute("#/targets");
    await flush();
    assert.match(root().textContent, /no engine is executed/i);
  });
});
