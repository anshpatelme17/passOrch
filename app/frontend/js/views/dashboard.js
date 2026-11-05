/*
 * Dashboard view (Phase 4B.1).
 *
 * Four independent sections, each with its own request, its own state and its
 * own retry. A failure in one section never blocks the others: every section
 * renders its own loading, empty, error or loaded state into a slot that is
 * created up front, so the panel structure is stable while data arrives.
 *
 * Endpoints used - nothing else is called from this module:
 *   GET  /health               -> {status, mode}
 *   GET  /api/engines          -> {engines: [...], ...}
 *   GET  /api/jobs             -> {jobs: [...], count}
 *   GET  /api/target-instances -> {instances: [...], count}
 *
 * SECURITY BOUNDARY
 * All network calls go through the shared client in js/api.js; this module
 * never calls fetch itself. All output is rendered through the textContent
 * helpers in js/dom.js, so no API string can become markup. Nothing here
 * triggers discovery, capability refresh or execution - the engine panel only
 * *reads* what the backend already holds, and says so when it holds nothing.
 */

import { api } from "../api.js";
import { registerView } from "../app.js";
import {
  CodeOrDash,
  EnginesTable,
  InlineError,
  NavLink,
  Panel,
  RetryButton,
  SAFE_FAILURE_MESSAGE,
  SimulatedOnlyBadge,
  StatusBadge,
} from "../components.js";
import { empty, h, kv, pending, render, table, text, asArray, formatTimestamp } from "../dom.js";

/* ---- section: health ------------------------------------------------------ */

async function healthSection(slot) {
  render(slot, pending("Checking application health..."));
  const res = await api.get("/health");
  if (!res.ok) {
    render(slot, [InlineError(res.error), RetryButton(() => runSection(healthSection, slot))]);
    return;
  }
  const data = res.data || {};
  // GET /health returns {status, mode} - both plain strings.
  render(slot, [
    kv([
      ["status", StatusBadge(data.status || "")],
      ["operating mode", h("span", { children: [SimulatedOnlyBadge()] })],
    ]),
    h("p", {
      class: "panel__hint",
      text: "mode: " + (data.mode || "unknown") + " - PassOrch plans and records simulated audits only.",
    }),
  ]);
}

/* ---- section: engines ----------------------------------------------------- */

async function enginesSection(slot) {
  render(slot, pending("Loading engine capability state..."));
  const res = await api.get("/api/engines");
  if (!res.ok) {
    render(slot, [InlineError(res.error), RetryButton(() => runSection(enginesSection, slot))]);
    return;
  }
  const engines = asArray(res.data && res.data.engines);
  if (engines.length === 0) {
    // Honest empty state: this dashboard itself never probes an engine and
    // offers no refresh control - the refresh button lives on the Engines
    // view (M1.3) and runs only on an explicit click. An empty list is the
    // expected result, not an error - and not something to invent engines for.
    render(slot, [
      empty(
        "No engine capability data available",
        "No engines have been registered yet. Capability information appears here only after an operator-side refresh; the dashboard never triggers discovery itself."
      ),
    ]);
    return;
  }
  render(slot, [
    EnginesTable(engines),
    h("p", {
      class: "panel__hint",
      text:
        "Read-only capability snapshot. No endpoint here can refresh or probe an engine, so this list only changes after a server-side refresh.",
    }),
  ]);
}

/* ---- section: recent jobs ------------------------------------------------- */

async function jobsSection(slot) {
  render(slot, pending("Loading recent jobs..."));
  const res = await api.get("/api/jobs");
  if (!res.ok) {
    render(slot, [InlineError(res.error), RetryButton(() => runSection(jobsSection, slot))]);
    return;
  }
  const jobs = asArray(res.data && res.data.jobs);
  const total = res.data && typeof res.data.total === "number" ? res.data.total : jobs.length;
  if (jobs.length === 0) {
    render(slot, [
      empty("No jobs recorded yet", "Simulated jobs appear here once one is submitted."),
      NavLink("Go to Jobs", "/jobs"),
    ]);
    return;
  }
  render(slot, [
    // JobSummary has exactly: job_id, status, created_at, target.
    table(
      ["Job", "Status", "Target", "Created"],
      jobs.slice(0, 5).map((job) => [
        CodeOrDash(job.job_id),
        StatusBadge(job.status || ""),
        text(job.target || "-"),
        formatTimestamp(job.created_at),
      ]),
      "Recent simulated jobs"
    ),
    h("p", {
      class: "panel__hint",
      text: `Showing ${Math.min(jobs.length, 5)} of ${total} recorded job(s), latest first.`,
    }),
    NavLink("Open Jobs", "/jobs"),
  ]);
}

/* ---- section: target instances -------------------------------------------- */

async function instancesSection(slot) {
  render(slot, pending("Loading target instances..."));
  const res = await api.get("/api/target-instances");
  if (!res.ok) {
    render(slot, [InlineError(res.error), RetryButton(() => runSection(instancesSection, slot))]);
    return;
  }
  const instances = asArray(res.data && res.data.instances);
  const total =
    res.data && typeof res.data.total === "number" ? res.data.total : instances.length;
  if (instances.length === 0) {
    render(slot, [
      empty(
        "No target instances defined",
        "A target instance names a specific thing to audit. Create one to associate simulated jobs with it."
      ),
      NavLink("Go to Instances", "/instances"),
    ]);
    return;
  }
  render(slot, [
    table(
      ["Name", "Id", "Target type", "Description"],
      instances.slice(0, 5).map((inst) => [
        text(inst.name || "-"),
        CodeOrDash(inst.id),
        StatusBadge(inst.target_type || ""),
        text(inst.description || "-"),
      ]),
      "Target instances"
    ),
    h("p", {
      class: "panel__hint",
      text: `Showing ${Math.min(instances.length, 5)} of ${total} target instance(s) defined.`,
    }),
    NavLink("Go to Instances", "/instances"),
  ]);
}

/** A compact statistic card used by the top summary row. */
function statCard(label, value, tone) {
  const valueEl = h("p", { class: "stat__value", text: String(value) });
  if (tone) {
    valueEl.setAttribute("data-tone", tone);
  }
  return h("div", { class: "stat", children: [
    h("p", { class: "stat__label", text: label }),
    valueEl,
  ] });
}

/* ---- section: statistics --------------------------------------------------- */

async function statisticsSection(slot) {
  render(slot, pending("Loading job statistics..."));
  const res = await api.get("/api/jobs/stats");
  if (!res.ok) {
    render(slot, [InlineError(res.error), RetryButton(() => runSection(statisticsSection, slot))]);
    return;
  }
  const data = res.data || {};
  const stats = [
    ["Total jobs", data.total ?? 0, undefined],
    ["Simulated", data.simulated ?? 0, "ok"],
    ["Denied", data.denied ?? 0, "deny"],
    ["Failed", data.failed ?? 0, "deny"],
  ];
  render(slot, h("div", {
    class: "stat-row",
    attrs: [["role", "status"], ["aria-label", "Job statistics"]],
    children: stats.map(([label, value, tone]) => statCard(label, value, tone)),
  }));
}

/* ---- view ----------------------------------------------------------------- */

/**
 * Render the dashboard.
 * @param {HTMLElement} root  the shared #view-root
 */
function renderDashboard(root) {
  // Build every section shell first, so all slots exist while their data is
  // in flight. Each slot is then filled independently. The grid puts the
  // workload (jobs, instances) in the main column and the system panels
  // (health, engines) in the side column.
  const healthSlot = h("div", { class: "section-body" });
  const enginesSlot = h("div", { class: "section-body" });
  const jobsSlot = h("div", { class: "section-body" });
  const instancesSlot = h("div", { class: "section-body" });
  const statsSlot = h("div", { class: "stat-slot" });

  render(root, [
    h("header", { class: "page-head", children: [
      h("h2", { class: "page-head__title", text: "Dashboard" }),
      h("p", {
        class: "page-head__hint",
        text:
          "Overview of simulated audits. Counts update as jobs complete; every section loads on its own and can be retried independently.",
      }),
    ] }),
    Panel("Job statistics", statsSlot, "Read-only aggregation over the recorded jobs. No engine was executed."),
    h("div", { class: "dashboard-grid", children: [
      h("div", { class: "dashboard-main", children: [
        Panel("Recent jobs", jobsSlot, "Up to the five most recent simulated jobs."),
        Panel("Target instances", instancesSlot, "Individual targets available for association."),
      ] }),
      h("div", { class: "dashboard-side", children: [
        Panel("Application health", healthSlot, "Unauthenticated readiness probe."),
        Panel("Engines", enginesSlot, "Capability state as currently held by the server."),
      ] }),
    ] }),
  ]);

  // Each section owns its request; none awaits the others, so a slow or failed
  // section cannot stall the rest of the page. A section that *rejects* (never
  // an HTTP error - those are handled inside each section) is caught here so
  // it cannot become an unhandled rejection, and only the fixed sentence below
  // reaches the DOM - never the exception text.
  runSection(healthSection, healthSlot);
  runSection(enginesSection, enginesSlot);
  runSection(statisticsSection, statsSlot);
  runSection(jobsSection, jobsSlot);
  runSection(instancesSection, instancesSlot);
}

/** Start `section` in `slot`, and convert a rejection into the safe state. */
function runSection(section, slot) {
  const safeFailure = () => {
    render(slot, [
      InlineError({ kind: "error", message: SAFE_FAILURE_MESSAGE }),
      RetryButton(() => runSection(section, slot)),
    ]);
  };
  try {
    // Sections are async, so this starts synchronously (the pending state is
    // visible immediately) and only the eventual rejection is caught here.
    Promise.resolve(section(slot)).catch(safeFailure);
  } catch {
    safeFailure();
  }
}

/* ---- registration --------------------------------------------------------- */

export const DashboardView = registerView("/dashboard", {
  title: "Dashboard",
  render: renderDashboard,
});

export default DashboardView;
