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
  SimulationNotice,
  SimulatedOnlyBadge,
  StatusBadge,
} from "../components.js";
import { empty, h, kv, pending, render, table, text } from "../dom.js";

/* ---- helpers -------------------------------------------------------------- */

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

/* ---- section: health ------------------------------------------------------ */

async function healthSection(slot) {
  render(slot, pending("Checking application health..."));
  const res = await api.get("/health");
  if (!res.ok) {
    render(slot, [InlineError(res.error), RetryButton(() => healthSection(slot))]);
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
    render(slot, [InlineError(res.error), RetryButton(() => enginesSection(slot))]);
    return;
  }
  const engines = asArray(res.data && res.data.engines);
  if (engines.length === 0) {
    // Honest empty state: no UI action can make the server probe an engine, and
    // Phase 3A deliberately exposes no refresh endpoint. An empty list is the
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
    render(slot, [InlineError(res.error), RetryButton(() => jobsSection(slot))]);
    return;
  }
  const jobs = asArray(res.data && res.data.jobs);
  const total = res.data && typeof res.data.count === "number" ? res.data.count : jobs.length;
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
        job.created_at === undefined ? "-" : String(job.created_at),
      ])
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
    render(slot, [InlineError(res.error), RetryButton(() => instancesSection(slot))]);
    return;
  }
  const instances = asArray(res.data && res.data.instances);
  const total =
    res.data && typeof res.data.count === "number" ? res.data.count : instances.length;
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
      ])
    ),
    h("p", {
      class: "panel__hint",
      text: `Showing ${Math.min(instances.length, 5)} of ${total} target instance(s) defined.`,
    }),
    NavLink("Go to Instances", "/instances"),
  ]);
}

/* ---- view ----------------------------------------------------------------- */

/**
 * Render the dashboard.
 * @param {HTMLElement} root  the shared #view-root
 */
function renderDashboard(root) {
  // Build every section shell first, so all four panels exist while their data
  // is still in flight. Each slot is then filled independently.
  const healthSlot = h("div", { class: "section-body" });
  const enginesSlot = h("div", { class: "section-body" });
  const jobsSlot = h("div", { class: "section-body" });
  const instancesSlot = h("div", { class: "section-body" });

  render(root, [
    SimulationNotice(),
    Panel(
      "Dashboard",
      [
        h("p", {
          class: "panel__hint",
          text:
            "Overview of the simulated PassOrch platform. Each section loads independently and can be retried on its own.",
        }),
      ]
    ),
    Panel("Application health", healthSlot),
    Panel("Engines", enginesSlot, "Capability state as currently held by the server."),
    Panel("Recent jobs", jobsSlot, "Up to the five most recent simulated jobs."),
    Panel("Target instances", instancesSlot, "Individual targets available for association."),
    Panel(
      "Simulation only",
      [
        h("p", {
          class: "panel__hint",
          text:
            "Every plan preview and job on this platform is a simulation. No engine binary is invoked, no attack is performed, and no capability data is probed from the browser.",
        }),
      ]
    ),
  ]);

  // Each section owns its request; none awaits the others, so a slow or failed
  // section cannot stall the rest of the page.
  healthSection(healthSlot);
  enginesSection(enginesSlot);
  jobsSection(jobsSlot);
  instancesSection(instancesSlot);
}

/* ---- registration --------------------------------------------------------- */

export const DashboardView = registerView("/dashboard", {
  title: "Dashboard",
  render: renderDashboard,
});

export default DashboardView;
