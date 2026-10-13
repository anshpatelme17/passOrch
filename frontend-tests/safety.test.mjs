import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { showRoute } from "../frontend/js/app.js";
import "../frontend/js/views/dashboard.js";
import "../frontend/js/views/targets.js";
import "../frontend/js/views/instances.js";
import "../frontend/js/views/plan.js";
import "../frontend/js/views/jobs.js";
import { flush, setup } from "./_fixtures.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
// frontend-tests -> repo root
const REPO_ROOT = dirname(HERE);
const JS_ROOT = join(REPO_ROOT, "frontend", "js");

/** The Phase 2D read-only library, pinned by its own suite. */
const LEGACY = "dashboard.js";

const MODULES = [
  "api.js",
  "dom.js",
  "components.js",
  "app.js",
  "main.js",
  "views/dashboard.js",
  "views/targets.js",
  "views/instances.js",
  "views/plan.js",
  "views/jobs.js",
];

const SINKS = [
  "innerHTML",
  "outerHTML",
  "insertAdjacentHTML",
  "document.write",
  "document.writeln",
  "eval(",
  "new Function",
  "setTimeout(",
  "setInterval(",
];

const EXEC_WORDS = [
  "subprocess",
  "popen",
  "spawn",
  "executable",
  "argv",
  "os.system",
  "child_process",
  "xmlhttprequest",
  "activexobject",
];

/** Strip comments so safety assertions cover only executable code. */
function code(src) {
  return src
    .replace(/\/\*.*?\*\//gs, "")
    .replace(/^[ \t]*\/\/[^\n]*$/gm, "");
}

function read(rel) {
  return readFileSync(join(JS_ROOT, rel), "utf8");
}

/**
 * Install the DOM + fetch environment once, up front, so the messaging tests
 * below can navigate the real router.
 */
function installEnv() {
  return setup({
    "GET /health": { status: 200, body: { status: "ok", mode: "simulated" } },
    "GET /api/targets": { status: 200, body: { targets: [], count: 0 } },
    "GET /api/jobs": { status: 200, body: { jobs: [], count: 0 } },
    "GET /api/target-instances": { status: 200, body: { instances: [], count: 0 } },
  });
}

installEnv();

describe("safety - unsafe DOM sinks", () => {
  for (const mod of MODULES) {
    it(`${mod} contains no HTML/exec sink in executable code`, () => {
      const c = code(read(mod));
      for (const sink of SINKS) {
        assert.equal(c.includes(sink), false, `${mod} uses ${sink}`);
      }
    });
  }
});

describe("safety - execution primitives", () => {
  for (const mod of MODULES) {
    it(`${mod} never references process execution`, () => {
      const c = code(read(mod));
      const lowered = c.toLowerCase();
      for (const word of EXEC_WORDS) {
        assert.equal(lowered.includes(word), false, `${mod} references ${word}`);
      }
    });
  }
});

describe("safety - network boundary", () => {
  it("only api.js calls fetch", () => {
    for (const mod of MODULES) {
      const c = code(read(mod));
      const calls = /\bfetch\s*\(/.test(c);
      if (mod === "api.js") {
        assert.equal(calls, true, "api.js must own fetch");
      } else {
        assert.equal(calls, false, `${mod} calls fetch`);
      }
    }
  });

  it("the API client only targets first-party paths", () => {
    const c = read("api.js");
    assert.match(c, /"\/api\/"/);
    assert.match(c, /"\/health"/);
  });

  it("the API client rejects absolute origins", async () => {
    let called = false;
    globalThis.fetch = async () => {
      called = true;
      return { status: 200, ok: true, text: async () => "{}" };
    };
    const { request } = await import("../frontend/js/api.js");
    const res = await request("http://evil.test/api/jobs");
    assert.equal(res.ok, false);
    assert.equal(res.error.kind, "invalid_request");
    assert.equal(called, false);
  });
});

describe("safety - legacy library untouched", () => {
  it("still exposes the Phase 2D read-only surface", () => {
    // `tests/test_dashboard_js.py` is the authoritative pin; this guards the
    // other direction - that the Phase 4B tree has not absorbed it.
    const legacy = read(LEGACY);
    assert.match(legacy, /PassOrchDashboard/);
    assert.match(legacy, /loadTargets/);
    assert.match(legacy, /previewPlan/);
    assert.match(legacy, /SECURITY BOUNDARY/);
  });

  it("is not imported by any new module", () => {
    for (const mod of MODULES) {
      const src = read(mod);
      for (const spec of ['"./dashboard.js"', '"../dashboard.js"', '"./js/dashboard.js"']) {
        assert.equal(src.includes(spec), false, `${mod} imports ${spec}`);
      }
    }
  });
});

describe("safety - simulation-only messaging", () => {
  it("the shell declares the notice", () => {
    const html = readFileSync(join(REPO_ROOT, "frontend/index.html"), "utf8");
    assert.match(html, /Simulation only/);
    assert.match(html, /no engine is ever executed/);
  });

  it("every implemented view restates it", async () => {
    for (const path of ["/dashboard", "/targets", "/instances", "/plan", "/jobs"]) {
      showRoute("#" + path);
      await flush();
      const text = globalThis.document.getElementById("view-root").textContent;
      assert.match(text, /Simulation only/, `${path} must keep the notice`);
    }
  });

  it("the Plan and Jobs views state nothing is executed", async () => {
    showRoute("#/jobs");
    await flush();
    const text = globalThis.document.getElementById("view-root").textContent;
    assert.match(text, /No engine binary is ever invoked/i);
  });
});

describe("safety - no fabricated engine behavior", () => {
  it("no view claims real cracking output", async () => {
    for (const path of ["/dashboard", "/jobs"]) {
      showRoute("#" + path);
      await flush();
      const text = globalThis.document.getElementById("view-root").textContent;
      for (const claim of ["cracked", "password found", "hashes recovered", "plaintext is"]) {
        assert.doesNotMatch(text, new RegExp(claim, "i"), `${path} claims ${claim}`);
      }
    }
  });

  it("the Engines route remains an honest placeholder", async () => {
    const info = showRoute("#/engines");
    assert.equal(info.rendered, false);
    await flush();
    const text = globalThis.document.getElementById("view-root").textContent;
    assert.match(text, /not implemented yet/);
  });
});

describe("safety - module hygiene", () => {
  it("all view modules are registered and imported", async () => {
    const { hasView } = await import("../frontend/js/app.js");
    for (const path of ["/dashboard", "/targets", "/instances", "/plan", "/jobs"]) {
      assert.equal(hasView(path), true, path);
    }
  });

  it("there are no stray files under frontend-tests/", () => {
    const allowed = new Set([
      "_dom-shim.mjs",
      "_fixtures.mjs",
      "_regression.test.mjs",
      "_fixtures.test.mjs",
      "api.test.mjs",
      "views.test.mjs",
      "dashboard.test.mjs",
      "targets.test.mjs",
      "instances.test.mjs",
      "plan.test.mjs",
      "jobs.test.mjs",
      "safety.test.mjs",
    ]);
    const found = readdirSync(HERE).filter((f) => f.endsWith(".mjs"));
    for (const f of found) {
      assert.ok(allowed.has(f), `unexpected file: ${f}`);
    }
  });

  it("all frontend files use LF line endings", () => {
    const walk = (dir) => {
      for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
          walk(full);
        } else if (entry.endsWith(".js") || entry.endsWith(".html") || entry.endsWith(".css")) {
          const raw = readFileSync(full);
          assert.equal(raw.includes(Buffer.from("\r\n")), false, full);
        }
      }
    };
    walk(join(REPO_ROOT, "frontend"));
  });
});
