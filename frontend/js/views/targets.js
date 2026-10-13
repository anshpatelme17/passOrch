/*
 * Targets screen (Phase 4B.2).
 *
 * A read-only catalog view over the static target-type policy. The catalog is
 * served by the backend and never inferred, guessed or synthesised here: every
 * row is rendered from the fields the API actually returns, and anything the
 * payload does not supply is shown as a dash rather than invented.
 *
 * Endpoint used - nothing else is called from this module:
 *   GET /api/targets -> {targets: [{target, description, formats, enabled,
 *                                   usable}], count}
 *
 * SECURITY BOUNDARY
 * All network calls go through the shared client in js/api.js; this module
 * never calls fetch itself. All output is rendered through the textContent
 * helpers in js/dom.js, so no API string can become markup. Nothing here
 * triggers discovery, capability refresh or engine execution - a "usable"
 * target is only an invitation to request a *simulated* preview.
 */

import { api } from "../api.js";
import { registerView } from "../app.js";
import {
  InlineError,
  NavLink,
  Panel,
  RetryButton,
  SimulationNotice,
} from "../components.js";
import { badge, empty, h, pending, render, table, text } from "../dom.js";

/* ---- helpers -------------------------------------------------------------- */

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

/** Render a target's formats as a comma-joined text node, or a dash. */
function formatsText(formats) {
  if (!Array.isArray(formats)) {
    return "-";
  }
  const joined = formats.filter((f) => typeof f === "string" && f.length > 0).join(", ");
  return joined || "-";
}

/**
 * A "preview plan" link. Only offered for a target the catalog marks usable:
 * a disabled or format-less target cannot produce a meaningful preview.
 */
function PlanLink(target) {
  if (!target || typeof target.target !== "string" || target.target.length === 0) {
    return badge("-", "muted");
  }
  return h("a", {
    class: "btn",
    attrs: [["href", `#/plan?target=${encodeURIComponent(target.target)}`]],
    text: "Preview plan",
  });
}

/**
 * Usable/unusable presentation.
 *
 * `usable` is read from the API (Enabled + at least one mapped format). The
 * current shipped catalog marks every entry usable, but the field is real and
 * a disabled or format-less target renders as unusable rather than being hidden
 * or silently assumed usable.
 */
function usableCell(target) {
  return target.usable === true ? badge("usable", "ok") : badge("not usable", "deny");
}

function enabledCell(target) {
  return target.enabled === true ? badge("enabled", "ok") : badge("disabled", "muted");
}

/* ---- view ----------------------------------------------------------------- */

async function renderTargets(root) {
  render(root, [pending("Loading target catalog...")]);

  const res = await api.get("/api/targets");
  if (!res.ok) {
    render(root, [
      SimulationNotice(),
      Panel("Targets", [
        h("div", { class: "section-body" }),
        InlineError(res.error),
        RetryButton(() => renderTargets(root), "Retry loading targets"),
      ]),
    ]);
    return;
  }

  const targets = asArray(res.data && res.data.targets);

  if (targets.length === 0) {
    render(root, [
      SimulationNotice(),
      Panel(
        "Targets",
        [
          h("div", { class: "section-body" }),
          empty(
            "No target types available",
            "The target catalog is empty. Target types come from the server-side policy map, so this screen shows nothing until that map has entries."
          ),
        ],
        undefined
      ),
    ]);
    return;
  }

  // Defensive: drop entries that are not objects with a usable string id, so a
  // malformed payload cannot produce a broken row or a bogus link.
  const valid = targets.filter(
    (t) => t !== null && typeof t === "object" && typeof t.target === "string" && t.target.length > 0
  );
  const usableCount = valid.filter((t) => t.usable === true).length;

  render(root, [
    SimulationNotice(),
    Panel(
      "Targets",
      [
        h("div", { class: "section-body" }),
        table(
          ["Target", "Description", "Formats", "Enabled", "Usable", "Plan"],
          valid.map((t) => [
            text(t.target),
            text(t.description || "-"),
            text(formatsText(t.formats)),
            enabledCell(t),
            usableCell(t),
            t.usable === true ? PlanLink(t) : badge("-", "muted"),
          ])
        ),
        h("p", {
          class: "panel__hint",
          text:
            `${valid.length} target type(s), ${usableCount} usable. ` +
            "A usable target can be used to request a simulated plan preview - no engine is executed.",
        }),
        NavLink("Open Plan Preview", "/plan"),
      ],
      "Static, read-only catalog of audit target types served by the backend."
    ),
  ]);
}

/* ---- registration --------------------------------------------------------- */

export const TargetsView = registerView("/targets", {
  title: "Targets",
  render: (root) => {
    renderTargets(root).catch((err) => {
      render(root, [SimulationNotice(), InlineError(err)]);
    });
  },
});

export default TargetsView;
