/**
 * Phase 2D - read-only plan dashboard.
 *
 * SECURITY BOUNDARY
 * This script only *reads* structured JSON produced by the PassOrch API:
 *
 *   GET  /api/targets                              -> target list
 *   POST /api/targets/{id}/compatibility/plan      -> plan preview
 *
 * It never builds, sends or renders a command, argv list or executable path.
 * It writes exclusively through `textContent` (no innerHTML, no eval, no
 * Function constructor, no document.write).
 */
(function (global) {
  "use strict";

  var TARGETS_URL = "/api/targets";
  var HEALTH_URL = "/health";

  function planUrl(targetId) {
    if (typeof targetId !== "string" || targetId.length === 0) {
      throw new Error("targetId is required");
    }
    if (!/^[a-z0-9-]+$/.test(targetId)) {
      throw new Error("targetId is not a known target identifier");
    }
    return TARGETS_URL + "/" + targetId + "/compatibility/plan";
  }

  async function getJson(url, options) {
    var response = await fetch(url, options);
    var payload = await response.json();
    return { ok: response.ok, status: response.status, payload: payload };
  }

  async function loadTargets() {
    var result = await getJson(TARGETS_URL);
    if (!result.ok) {
      throw new Error("target list unavailable");
    }
    return result.payload.targets || [];
  }

  async function previewPlan(targetId, options) {
    var opts = options || {};
    var body = {
      mode: typeof opts.mode === "string" ? opts.mode : "simulated",
      reference: typeof opts.reference === "string" ? opts.reference : ""
    };
    var result = await getJson(planUrl(targetId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!result.ok) {
      throw new Error("plan preview rejected by the API");
    }
    return result.payload;
  }

  function clear(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function line(label, value) {
    var row = document.createElement("div");
    row.className = "plan-row";
    var key = document.createElement("span");
    key.className = "plan-key";
    key.textContent = label;
    var val = document.createElement("span");
    val.className = "plan-value";
    val.textContent = value;
    row.appendChild(key);
    row.appendChild(val);
    return row;
  }

  function section(title) {
    var head = document.createElement("h3");
    head.textContent = title;
    return head;
  }

  function renderPlan(container, preview) {
    clear(container);
    container.appendChild(section("Target"));
    container.appendChild(line("target", preview.target));
    container.appendChild(line("selection", preview.selection.status));
    container.appendChild(
      line("selected engine", preview.selection.selected_engine || "-")
    );

    container.appendChild(section("Authorization"));
    container.appendChild(line("status", preview.authorization.status));
    container.appendChild(line("reason", preview.authorization.reason));
    if (preview.authorization.detail) {
      container.appendChild(line("detail", preview.authorization.detail));
    }
    container.appendChild(line("mode", preview.authorization.mode || "-"));

    if (preview.plan) {
      container.appendChild(section("Simulation plan"));
      container.appendChild(line("adapter", preview.plan.adapter));
      preview.plan.steps.forEach(function (step) {
        container.appendChild(line("step", step));
      });
    } else {
      container.appendChild(section("Simulation plan"));
      container.appendChild(line("plan", "none (denied)"));
    }

    if (preview.simulation) {
      container.appendChild(section("Simulation result"));
      container.appendChild(line("status", preview.simulation.status));
      container.appendChild(line("simulation id", preview.simulation.simulation_id));
      container.appendChild(line("message", preview.simulation.message));
    }
  }

  function renderTargets(container, targets) {
    clear(container);
    targets.forEach(function (entry) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "target-item";
      item.dataset.targetId = entry.target;
      item.textContent = entry.target + " - " + entry.description;
      container.appendChild(item);
    });
  }

  function errorMessage(container, err) {
    clear(container);
    container.appendChild(line("error", String(err && err.message ? err.message : err)));
  }

  async function init(config) {
    var cfg = config || {};
    var listEl = document.querySelector(cfg.listSelector || "#target-list");
    var planEl = document.querySelector(cfg.planSelector || "#plan-preview");
    if (!listEl || !planEl) {
      return null;
    }
    var targets = await loadTargets();
    renderTargets(listEl, targets);
    listEl.addEventListener("click", async function (event) {
      var button = event.target.closest("[data-target-id]");
      if (!button) {
        return;
      }
      try {
        var preview = await previewPlan(button.dataset.targetId, cfg.options);
        renderPlan(planEl, preview);
      } catch (err) {
        errorMessage(planEl, err);
      }
    });
    if (targets.length > 0) {
      try {
        renderPlan(planEl, await previewPlan(targets[0].target, cfg.options));
      } catch (err) {
        errorMessage(planEl, err);
      }
    }
    return targets;
  }

  global.PassOrchDashboard = {
    HEALTH_URL: HEALTH_URL,
    TARGETS_URL: TARGETS_URL,
    init: init,
    loadTargets: loadTargets,
    previewPlan: previewPlan,
    renderPlan: renderPlan,
    renderTargets: renderTargets,
    planUrl: planUrl
  };
})(typeof window !== "undefined" ? window : this);
