/*
 * Engines view (Phase 6A / M1.3 - user-initiated discovery refresh).
 *
 * Shows engine capability state and lets an operator run a metadata
 * discovery refresh. The refresh is *only* ever started by an explicit
 * click on the "Refresh engines" button - never on page load, never on a
 * timer, never automatically after any other request.
 *
 * Endpoints used - nothing else is called from this module:
 *   GET  /api/engines           -> {engines: [EngineInfo, ...]}
 *   POST /api/engines/refresh   -> {engines: [EngineRefreshReport, ...]}
 *
 * The POST is sent with NO body: this view cannot and does not supply
 * engine names, paths, commands, operations, timeouts, output limits or
 * any other parameter. What runs server-side is allowlisted metadata
 * discovery (engine presence + supported capabilities) - never a password
 * audit, never a cracking operation.
 *
 * SECURITY BOUNDARY
 * All network calls go through the shared client in js/api.js; this module
 * never calls fetch itself, never stores credentials or tokens, never
 * polls, and renders only through the textContent helpers in js/dom.js, so
 * no API string can become markup. The ``real_engine_compatible`` badge
 * means the engine adapter reports compatibility with a real engine binary
 * - it does NOT mean real execution is enabled. VERIFIED capability status
 * means verified metadata evidence, nothing more.
 */

import { api } from "../api.js";
import { registerView } from "../app.js";
import {
  BoolBadge,
  EnginesTable,
  InlineError,
  NavLink,
  Panel,
  RetryButton,
  SAFE_FAILURE_MESSAGE,
  StatusBadge,
  SimulationNotice,
} from "../components.js";
import { empty, h, pending, render, table, text } from "../dom.js";

/* ---- copy ---------------------------------------------------------------- */

/** The one sentence that must always travel with a refresh affordance. */
const DISCOVERY_WORDING =
  "Discovery checks installed engine metadata and supported capabilities. " +
  "It does not perform password auditing or cracking.";

const LIST_FAILED_TITLE = "Discovery completed, but the engine list could not be updated.";

/** Structured error code -> a short, safe operator hint. No internals. */
const REFRESH_ERROR_HINTS = {
  unauthorized:
    "This server requires an operator token for refresh, and the browser interface cannot supply one. Refresh from a token-aware client (for example curl with the configured bearer token) instead.",
  refresh_in_progress: "Another discovery refresh is already running. Wait for it to finish, then retry.",
  rate_limited: "Too many refresh requests. Wait for the retry window, then try again.",
  discovery_refresh_failed:
    "Discovery refresh is unavailable on the server right now. Nothing was changed.",
  unknown_engine: "The server rejected the engine selection. Nothing was changed.",
};

/* ---- helpers -------------------------------------------------------------- */

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function isReport(entry) {
  return entry !== null && typeof entry === "object" && typeof entry.engine === "string" && entry.engine.length > 0;
}

/** A safe, per-status hint for a failed refresh. Keyed on the machine code
 *  the backend actually returns; unknown codes fall back to a generic
 *  sentence, so no internal exception text is ever needed or shown. */
function refreshFailureHint(res) {
  if (res.error && res.error.kind === "transport") {
    return "Cannot reach the PassOrch API. Check that the server is running, then retry.";
  }
  const code = res.error && typeof res.error.code === "string" ? res.error.code : "";
  if (code && Object.prototype.hasOwnProperty.call(REFRESH_ERROR_HINTS, code)) {
    return REFRESH_ERROR_HINTS[code];
  }
  if (res.status === 422) {
    return "The request was rejected as invalid by the server. Nothing was changed.";
  }
  return "The refresh did not run. The engine list below is unchanged.";
}

/* ---- capability status legend -------------------------------------------- */

function statusLegend() {
  return h("div", {
    class: "stack",
    children: [
      h("p", {
        class: "panel__hint",
        text: "VERIFIED: supported capability metadata was verified for this engine - not that real execution is enabled.",
      }),
      h("p", {
        class: "panel__hint",
        text: "UNKNOWN: no verified capability evidence is held (for example the engine binary is not installed).",
      }),
      h("p", {
        class: "panel__hint",
        text: "ERROR: the last discovery attempt for this engine failed.",
      }),
    ],
  });
}

/* ---- refresh outcome ------------------------------------------------------ */

/**
 * Per-engine refresh reports (RefreshEnginesResponse.engines). A partial
 * success is a success with detail - never presented as a total failure:
 * each engine's own outcome is reported, and any engine without VERIFIED
 * evidence is named explicitly.
 */
function refreshOutcomeNodes(reports) {
  const verified = reports.filter((r) => r.capability_status === "verified");
  const unverified = reports.filter((r) => r.capability_status !== "verified");
  const title =
    reports.length === 0
      ? "Metadata discovery finished: no engines were refreshed."
      : unverified.length === 0
        ? `Metadata discovery finished: all ${reports.length} engine(s) hold verified capability evidence.`
        : `Metadata discovery finished: ${verified.length} of ${reports.length} engine(s) hold verified capability evidence.`;

  const kids = [
    h("div", {
      class: "state",
      attrs: [["role", "status"]],
      children: [
        h("p", { class: "state__title", text: title }),
        h("p", { class: "panel__hint", text: DISCOVERY_WORDING }),
      ],
    }),
  ];

  for (const report of unverified) {
    const reason = report.reason ? `: ${report.reason}` : "";
    kids.push(
      h("p", {
        class: "panel__hint",
        text: `${report.engine} remains unverified (${report.capability_status || "unknown"}${reason}).`,
      })
    );
  }

  // Truncated discovery output: flag incomplete evidence for the operator
  // without touching the backend's own capability status - no new status is
  // invented, no raw probe output is shown, and this is a caveat on the
  // evidence, never a failure of the refresh as a whole.
  for (const report of reports) {
    if (report.truncated !== true) {
      continue;
    }
    kids.push(
      h("p", {
        class: "panel__hint",
        text:
          `Truncated discovery output for ${report.engine}: the captured metadata was incomplete, ` +
          "so this evidence must not be interpreted as verified capability evidence. " +
          `The reported capability status (${report.capability_status || "unknown"}) is unchanged.`,
      })
    );
  }

  if (reports.length > 0) {
    kids.push(
      table(
        ["Engine", "Discovered", "Capability", "Reason"],
        reports.map((r) => [
          text(r.engine || "-"),
          BoolBadge(r.discovered === true, "yes", "no"),
          StatusBadge(r.capability_status || ""),
          text(r.reason || "-"),
        ])
      )
    );
  }
  return kids;
}

/* ---- view ----------------------------------------------------------------- */

async function renderEngines(root) {
  // Reset the in-flight guard on entry: a refresh still pending from a
  // previous visit runs against detached slots, and if it never settles the
  // flag would stay latched - disabling this screen's only action for the
  // rest of the session.
  refreshing = false;

  const ctx = {
    controlsSlot: h("div", { class: "section-body" }),
    resultSlot: h("div", { class: "section-body" }),
    listSlot: h("div", { class: "section-body" }),
  };

  render(root, [
    SimulationNotice(),
    Panel(
      "Engines",
      [
        h("p", { class: "panel__hint", text: DISCOVERY_WORDING }),
        ctx.controlsSlot,
        ctx.resultSlot,
        ctx.listSlot,
      ],
      "Engine capability state as currently held by the server. Refresh runs only when you click the button."
    ),
  ]);

  renderControls(ctx, false);
  await reloadList(ctx);
}

/** The refresh button. Disabled (and guarded by `refreshing`) while a
 *  refresh is in flight, so repeated clicks cannot queue requests. */
function renderControls(ctx, busy) {
  render(ctx.controlsSlot, [
    h("div", {
      class: "actions",
      children: [
        h("button", {
          type: "button",
          class: "btn btn--primary",
          text: "Refresh engines",
          attrs: [["data-refresh-button", "true"]].concat(busy ? [["disabled", ""]] : []),
          onClick: () => runRefresh(ctx),
        }),
      ],
    }),
  ]);
}

/** GET /api/engines -> the capability table (or the honest empty state).
 *  `res` may be an already-fetched list response, avoiding a second GET
 *  right after a refresh. */
async function reloadList(ctx, res) {
  if (!res) {
    render(ctx.listSlot, [pending("Loading engine capability state...")]);
    res = await api.get("/api/engines");
  }
  if (!res.ok) {
    render(ctx.listSlot, [InlineError(res.error), RetryButton(() => reloadList(ctx))]);
    return;
  }

  const engines = asArray(res.data && res.data.engines).filter(
    (e) => e !== null && typeof e === "object" && typeof e.engine === "string" && e.engine.length > 0
  );

  if (engines.length === 0) {
    render(ctx.listSlot, [
      empty(
        "No engine capability data available",
        'No engines have been registered yet. Click "Refresh engines" to run metadata discovery; results appear here once the server holds capability records.'
      ),
      statusLegend(),
      NavLink("Back to Dashboard", "/dashboard"),
    ]);
    return;
  }

  render(ctx.listSlot, [
    EnginesTable(engines),
    statusLegend(),
    h("p", {
      class: "panel__hint",
      text:
        '"Real-engine compatible" means the adapter reports compatibility with a real engine binary. It does NOT mean real execution is enabled - this platform is simulation-only.',
    }),
    NavLink("Back to Dashboard", "/dashboard"),
  ]);
}

/* ---- refresh --------------------------------------------------------------- */

/** In-flight guard: one refresh at a time per page, on top of the server's
 *  own 409 lock. */
let refreshing = false;

async function runRefresh(ctx) {
  if (refreshing) {
    return;
  }
  refreshing = true;
  try {
    renderControls(ctx, true);
    render(ctx.resultSlot, [pending("Discovering engine metadata...")]);

    // Deliberately body-less: no engine names, paths, commands, timeouts or
    // limits are ever supplied from here. The server refreshes all known
    // engines with its own allowlisted operations.
    const res = await api.post("/api/engines/refresh");

    if (!res.ok) {
      render(ctx.resultSlot, [
        InlineError(res.error),
        h("p", { class: "panel__hint", text: refreshFailureHint(res) }),
      ]);
      return;
    }

    const reports = asArray(res.data && res.data.engines).filter(isReport);
    const listRes = await api.get("/api/engines");
    if (!listRes.ok) {
      render(ctx.resultSlot, [
        ...refreshOutcomeNodes(reports),
        h("p", { class: "state__title", text: LIST_FAILED_TITLE }),
        InlineError(listRes.error),
        h("p", {
          class: "panel__hint",
          text: "The refresh itself succeeded; only the list below could not be reloaded.",
        }),
        RetryButton(() => {
          // Retrying clears the stale list-failure notice; the per-engine
          // discovery outcome above it stays, because it is still true.
          render(ctx.resultSlot, refreshOutcomeNodes(reports));
          reloadList(ctx);
        }, "Retry loading the engine list"),
      ]);
      return;
    }

    render(ctx.resultSlot, refreshOutcomeNodes(reports));
    await reloadList(ctx, listRes);
  } catch {
    // Rejections only: every HTTP failure is handled above with its own safe
    // hint. A fixed sentence is shown instead of the exception text, the
    // controls are re-enabled by the `finally`, and the list keeps whatever
    // it last held.
    render(ctx.resultSlot, [
      InlineError({ kind: "error", message: SAFE_FAILURE_MESSAGE }),
      h("p", { class: "panel__hint", text: "The refresh did not run. The engine list below is unchanged." }),
    ]);
  } finally {
    refreshing = false;
    renderControls(ctx, false);
  }
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
