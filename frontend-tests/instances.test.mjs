import assert from "node:assert/strict";
import { describe, it, beforeEach } from "node:test";

import { showRoute } from "../frontend/js/app.js";
import "../frontend/js/views/instances.js";
import {
  CATALOG,
  FakeResponse,
  click,
  containing,
  flush,
  selectValues,
  setup,
  type,
} from "./_fixtures.mjs";

let env;

const EMPTY = { instances: [], count: 0 };
const ONE = {
  instances: [
    {
      id: "33333333-3333-4333-8333-333333333333",
      name: "Contract PDF",
      target_type: "pdf-document",
      description: "signed",
      created_at: 1700000005,
    },
  ],
  count: 1,
};

function start(routes) {
  env = setup({
    "GET /api/targets": { status: 200, body: CATALOG },
    "GET /api/target-instances": { status: 200, body: ONE },
    ...routes,
  });
  return env;
}

function root() {
  return globalThis.document.getElementById("view-root");
}

function panel(title) {
  return containing(root(), title);
}

/** The <select> inside the create form, by its id. */
function select(id) {
  return root().querySelectorAll(`[id="${id}"]`)[0];
}

/** Elements that respond to `input`. */
function field(el) {
  return el;
}

beforeEach(() => {
  start();
});

describe("instances - list", () => {
  it("renders the existing instances", async () => {
    showRoute("#/instances");
    await flush();
    assert.match(panel("Target instances").textContent, /Contract PDF/);
    assert.match(root().textContent, /1 target instance/);
  });

  it("renders an empty state that prompts creation", async () => {
    start({ "GET /api/target-instances": { status: 200, body: EMPTY } });
    showRoute("#/instances");
    await flush();
    assert.match(root().textContent, /No target instances/);
    assert.match(root().textContent, /Create one/);
  });

  it("requests the catalog for the type picker", async () => {
    showRoute("#/instances");
    await flush();
    assert.ok(env.calls.some((c) => c.url === "/api/targets"));
  });
});

describe("instances - create form", () => {
  it("offers the real target types from the catalog", async () => {
    showRoute("#/instances");
    await flush();
    const values = selectValues(select("instance-type"));
    assert.deepEqual(values, ["", "office-document", "pdf-document", "zip-archive"]);
  });

  it("disables submit when the catalog is empty", async () => {
    start({ "GET /api/targets": { status: 200, body: { targets: [], count: 0 } } });
    showRoute("#/instances");
    await flush();
    const submit = root()
      .querySelectorAll("button")
      .find((b) => /Create target instance/.test(b.textContent));
    assert.ok(submit, "the form must still render with an empty catalog");
    assert.equal(submit.hasAttribute("disabled"), true);
    assert.match(root().textContent, /No target types available/);
  });

  it("regression: renders with an empty catalog instead of throwing", async () => {
    // `renderForm` used to reassign a const, which threw in strict mode.
    start({ "GET /api/targets": { status: 200, body: { targets: [], count: 0 } } });
    showRoute("#/instances");
    await flush();
    assert.doesNotMatch(root().textContent, /Assignment to constant variable/);
    assert.doesNotMatch(root().textContent, /This section failed to load/);
  });

  it("blocks submission client-side when no target type is chosen", async () => {
    showRoute("#/instances");
    await flush();
    // the button is enabled, so the client-side rule must catch it
    const submit = root()
      .querySelectorAll("button")
      .find((b) => /Create target instance/.test(b.textContent));
    assert.ok(submit);
    click(submit);
    await flush();
    assert.match(root().textContent, /Please fix the following before submitting/);
    assert.match(root().textContent, /Name is required/);
    assert.match(root().textContent, /A target type is required/);
    // and no request was made
    assert.equal(
      env.calls.filter((c) => c.method === "POST").length,
      0,
      "validation must precede the request"
    );
  });

  it("blocks submission when the name is blank", async () => {
    showRoute("#/instances");
    await flush();
    // choose a target so only the name is missing
    const typeSel = select("instance-type");
    typeSel.value = "pdf-document";
    typeSel.dispatchEvent({ type: "change", target: typeSel });

    const submit = root().querySelectorAll("button").find((b) => /Create target instance/.test(b.textContent));
    click(submit);
    await flush();
    assert.match(root().textContent, /Please fix the following before submitting/);
    assert.match(root().textContent, /Name is required/);
  });

  it("blocks submission when the target type is missing", async () => {
    showRoute("#/instances");
    await flush();
    const submit = root()
      .querySelectorAll("button")
      .find((b) => /Create target instance/.test(b.textContent));
    click(submit);
    await flush();
    // the client-side rule refuses a blank type
    assert.match(root().textContent, /A target type is required/);
    assert.equal(
      env.calls.filter((c) => c.method === "POST").length,
      0,
      "an invalid form must never reach the API"
    );
  });

  it("submits and reports success", async () => {
    start({
      "POST /api/target-instances": {
        status: 201,
        body: {
          id: "88888888-8888-4888-8888-888888888888",
          name: "New archive",
          target_type: "pdf-document",
          description: "desc",
          created_at: 1700000099,
        },
      },
    });
    showRoute("#/instances");
    await flush();
    type(select("instance-name"), "New archive");
    const typeSel = select("instance-type");
    typeSel.value = "pdf-document";
    typeSel.dispatchEvent({ type: "change", target: typeSel });
    type(select("instance-description"), "desc");

    const submit = root()
      .querySelectorAll("button")
      .find((b) => /Create target instance/.test(b.textContent));
    click(submit);
    await flush();

    const posted = env.calls.find((c) => c.method === "POST");
    assert.ok(posted, "a POST must be issued");
    assert.equal(posted.body.name, "New archive");
    assert.equal(posted.body.target_type, "pdf-document");
    assert.equal(posted.body.description, "desc");
    // only the three schema keys are sent
    assert.deepEqual(Object.keys(posted.body).sort(), ["description", "name", "target_type"]);
    assert.match(root().textContent, /Created target instance "New archive"/);
  });

  it("omits instance_id from the job payload when unset", async () => {
    showRoute("#/instances");
    await flush();
    type(select("instance-name"), "x");
    const typeSel = select("instance-type");
    typeSel.value = "pdf-document";
    typeSel.dispatchEvent({ type: "change", target: typeSel });
    click(root().querySelectorAll("button").find((b) => /Create target instance/.test(b.textContent)));
    await flush(12);
    const posted = env.calls.find((c) => c.method === "POST");
    assert.ok(posted);
    assert.equal("instance_id" in posted.body, false);
  });

  it("refreshes the list after a successful create", async () => {
    start({
      "POST /api/target-instances": {
        status: 201,
        body: {
          id: "77777777-7777-4777-8777-777777777777",
          name: "Fresh",
          target_type: "pdf-document",
          description: "",
          created_at: 1700000099,
        },
      },
      "GET /api/target-instances": { status: 200, body: ONE },
    });
    showRoute("#/instances");
    await flush();
    const before = env.calls.filter((c) => c.url === "/api/target-instances").length;
    type(select("instance-name"), "Fresh");
    const typeSel = select("instance-type");
    typeSel.value = "pdf-document";
    typeSel.dispatchEvent({ type: "change", target: typeSel });
    click(root().querySelectorAll("button").find((b) => /Create target instance/.test(b.textContent)));
    await flush(14);
    const after = env.calls.filter((c) => c.url === "/api/target-instances").length;
    assert.ok(after > before, "the list must be re-fetched after a successful create");
  });

  it("keeps the entered values when the create fails", async () => {
    start({
      "POST /api/target-instances": { status: 422, body: { detail: "name must be non-empty" } },
    });
    showRoute("#/instances");
    await flush();
    type(select("instance-name"), "Keep me");
    const typeSel = select("instance-type");
    typeSel.value = "pdf-document";
    typeSel.dispatchEvent({ type: "change", target: typeSel });
    click(root().querySelectorAll("button").find((b) => /Create target instance/.test(b.textContent)));
    await flush(14);
    assert.match(root().textContent, /name must be non-empty/);
    // the form is still present with its input
    assert.equal(select("instance-name").value, "Keep me");
  });

  it("clears the form only after a successful create", async () => {
    start({
      "POST /api/target-instances": {
        status: 201,
        body: {
          id: "77777777-7777-4777-8777-777777777777",
          name: "Fresh",
          target_type: "pdf-document",
          description: "",
          created_at: 1,
        },
      },
    });
    showRoute("#/instances");
    await flush();
    type(select("instance-name"), "Fresh");
    const typeSel = select("instance-type");
    typeSel.value = "pdf-document";
    typeSel.dispatchEvent({ type: "change", target: typeSel });
    click(root().querySelectorAll("button").find((b) => /Create target instance/.test(b.textContent)));
    await flush(14);
    assert.equal(select("instance-name").value, "");
  });
});

describe("instances - delete", () => {
  it("requires a confirmation click before deleting", async () => {
    showRoute("#/instances");
    await flush();
    const del = root().querySelectorAll("button").find((b) => b.textContent === "Delete");
    assert.ok(del, "the row must offer Delete");
    click(del);
    await flush();
    assert.match(root().textContent, /Delete target instance "Contract PDF"\?/);
    assert.match(root().textContent, /Jobs already recorded against it are not affected/);
    // no DELETE has been issued yet
    assert.equal(env.calls.filter((c) => c.method === "DELETE").length, 0);
  });

  it("cancel returns to the list without deleting", async () => {
    showRoute("#/instances");
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Delete"));
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Cancel"));
    await flush(10);
    assert.equal(env.calls.filter((c) => c.method === "DELETE").length, 0);
    assert.match(root().textContent, /Contract PDF/);
  });

  it("issues the DELETE on confirmation and handles 204", async () => {
    start({
      "DELETE /api/target-instances/*": { status: 204 },
    });
    showRoute("#/instances");
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Delete"));
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Yes, delete"));
    await flush(14);

    const del = env.calls.find((c) => c.method === "DELETE");
    assert.ok(del, "the DELETE must be issued only after confirmation");
    assert.equal(del.url, "/api/target-instances/33333333-3333-4333-8333-333333333333");
    assert.match(root().textContent, /Deleted target instance "Contract PDF"/);
  });

  it("refreshes the list after a successful delete", async () => {
    start({ "DELETE /api/target-instances/*": { status: 204 } });
    showRoute("#/instances");
    await flush();
    const before = env.calls.filter((c) => c.url.startsWith("/api/target-instances") && c.method === "GET").length;
    click(root().querySelectorAll("button").find((b) => b.textContent === "Delete"));
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Yes, delete"));
    await flush(14);
    const after = env.calls.filter((c) => c.url.startsWith("/api/target-instances") && c.method === "GET").length;
    assert.ok(after > before, "the list must refresh after a successful delete");
  });

  it("does not report success when the delete fails", async () => {
    start({
      "DELETE /api/target-instances/*": { status: 404, body: { detail: "target_instance_not_found" } },
    });
    showRoute("#/instances");
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Delete"));
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Yes, delete"));
    await flush(14);
    const text = root().textContent;
    assert.match(text, /target_instance_not_found/);
    assert.doesNotMatch(text, /Deleted target instance/);
    assert.match(text, /was not deleted/);
  });
});

describe("instances - edit", () => {
  it("opens a prefilled form for the mutable fields only", async () => {
    showRoute("#/instances");
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Edit"));
    await flush();
    assert.match(root().textContent, /Edit target instance "Contract PDF"/);
    assert.match(root().textContent, /Only the name and description can be changed/);
    assert.equal(select("instance-edit-name").value, "Contract PDF");
    assert.equal(select("instance-edit-description").value, "signed");
    // no PATCH has been issued yet
    assert.equal(env.calls.filter((c) => c.method === "PATCH").length, 0);
  });

  it("issues a PATCH with only the mutable fields and refreshes", async () => {
    start({
      "PATCH /api/target-instances/*": {
        status: 200,
        body: {
          id: "33333333-3333-4333-8333-333333333333",
          name: "Renamed PDF",
          target_type: "pdf-document",
          description: "signed v2",
          created_at: 1700000005,
        },
      },
    });
    showRoute("#/instances");
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Edit"));
    await flush();
    type(select("instance-edit-name"), "Renamed PDF");
    type(select("instance-edit-description"), "signed v2");
    const before = env.calls.filter((c) => c.url.startsWith("/api/target-instances") && c.method === "GET").length;
    click(root().querySelectorAll("button").find((b) => b.textContent === "Save changes"));
    await flush(14);

    const patch = env.calls.find((c) => c.method === "PATCH");
    assert.ok(patch, "the PATCH must be issued");
    assert.equal(patch.url, "/api/target-instances/33333333-3333-4333-8333-333333333333");
    assert.deepEqual(patch.body, { name: "Renamed PDF", description: "signed v2" });
    assert.match(root().textContent, /Updated target instance "Renamed PDF"/);
    const after = env.calls.filter((c) => c.url.startsWith("/api/target-instances") && c.method === "GET").length;
    assert.ok(after > before, "the list must refresh after a successful update");
  });

  it("cancel returns to the list without patching", async () => {
    showRoute("#/instances");
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Edit"));
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Cancel"));
    await flush(10);
    assert.equal(env.calls.filter((c) => c.method === "PATCH").length, 0);
    assert.match(root().textContent, /Contract PDF/);
  });

  it("validates the bounds client-side and never issues the PATCH", async () => {
    showRoute("#/instances");
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Edit"));
    await flush();
    type(select("instance-edit-name"), "   ");
    click(root().querySelectorAll("button").find((b) => b.textContent === "Save changes"));
    await flush(10);
    assert.equal(env.calls.filter((c) => c.method === "PATCH").length, 0);
    assert.match(root().textContent, /Name is required/);
  });

  it("keeps the form and reports the error when the PATCH fails", async () => {
    start({
      "PATCH /api/target-instances/*": {
        status: 422,
        body: { detail: "name must be a non-empty string" },
      },
    });
    showRoute("#/instances");
    await flush();
    click(root().querySelectorAll("button").find((b) => b.textContent === "Edit"));
    await flush();
    type(select("instance-edit-name"), "Renamed PDF");
    const before = env.calls.filter((c) => c.url.startsWith("/api/target-instances") && c.method === "GET").length;
    click(root().querySelectorAll("button").find((b) => b.textContent === "Save changes"));
    await flush(14);

    const text = root().textContent;
    assert.match(text, /name must be a non-empty string/);
    assert.doesNotMatch(text, /Updated target instance/);
    assert.match(text, /Edit target instance/, "the form must stay open for a retry");
    assert.equal(select("instance-edit-name").value, "Renamed PDF");
    const after = env.calls.filter((c) => c.url.startsWith("/api/target-instances") && c.method === "GET").length;
    assert.equal(after, before, "a failed update must not refresh the list");
  });
});

describe("instances - rendering and safety", () => {
  it("keeps the simulation-only notice in every state", async () => {
    for (const routes of [
      {},
      { "GET /api/target-instances": { status: 200, body: EMPTY } },
    ]) {
      start(routes);
      showRoute("#/instances");
      await flush();
      assert.match(root().textContent, /Simulation only/);
    }
  });

  it("renders the instance target type as a badge", async () => {
    showRoute("#/instances");
    await flush();
    assert.match(root().textContent, /pdf-document/);
  });

  it("links back to the dashboard", async () => {
    showRoute("#/instances");
    await flush();
    assert.ok(root().querySelectorAll("a").some((a) => a.getAttribute("href") === "#/dashboard"));
  });
});

void field;
void FakeResponse;
void env;
