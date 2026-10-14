/*
 * Engines view (Phase 6A).
 *
 * Read-only display of engine capability state. The view only *reads* what the
 * backend already holds - it never triggers discovery, capability refresh or
 * execution.
 *
 * Endpoint used - nothing else is called from this module:
 *   GET /api/engines -> {engines: [...], ...}
 *
 * SECURITY BOUNDARY
 * All network calls go through the shared client in js/api.js; this module
 * never calls fetch itself. All output is rendered through the textContent
 * helpers in js/dom.js, so no API string can become markup. The
 * ``real_engine_compatible`` badge means the engine adapter reports
 * compatibility with a real engine binary - it does NOT mean real execution
 * is enabled. No engine is ever executed by this platform.
 */

import { api } from "../api.js";
import { registerView } from "../app.js";
import {
  EnginesTable,
  InlineError,
  NavLink,
  Panel,
  RetryButton,
  SimulationNotice,
} from "../components.js";
import { empty, h, pending, render, text } from "../dom.js";

/* ---- helpers -------------------------------------------------------------- */

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

/* ---- view ----------------------------------------------------------------- */

async function renderEngines(root) {
  const slot = h("div", { class: "section-body" });

  render(root, [
    SimulationNotice(),
    Panel(
      "Engines",
      [
        h("div", { class: "section-body" }),
        h("p", {
          class: "panel__hint",
          text:
            "Read-only capability snapshot. This view never triggers discovery or refresh - it only shows what the server already holds.",
        }),
        slot,
      ],
      "Engine capability state as currently held by the server."
    ),
  ]);

  render(slot, pending("Loading engine capability state..."));
  const res = await api.get("/api/engines");
  if (!res.ok) {
    render(slot, [InlineError(res.error), RetryButton(() => renderEngines(root))]);
    return;
  }

  const engines = asArray(res.data && res.data.engines).filter(
    (e) => e !== null && typeof e === "object" && typeof e.engine === "string" && e.engine.length > 0
  );

  if (engines.length === 0) {
    render(slot, [
      empty(
        "No engine capability data available",
        "No engines have been registered yet. Capability information appears here only after an operator-side refresh; this view never triggers discovery itself."
      ),
      NavLink("Back to Dashboard", "/dashboard"),
    ]);
    return;
  }

  render(slot, [
    EnginesTable(engines),
    h("p", {
      class: "panel__hint",
      text:
        '"Real-engine compatible" means the adapter reports compatibility with a real engine binary. It does NOT mean real execution is enabled - this platform is simulation-only.',
    }),
    NavLink("Back to Dashboard", "/dashboard"),
  ]);
}

/* ---- registration --------------------------------------------------------- */

export const EnginesView = registerView("/engines", {
  title: "Engines",
  render: (root) => {
    renderEngines(root).catch((err) => {
      render(root, [SimulationNotice(), InlineError(err)]);
    });
  },
});

export default EnginesView;
