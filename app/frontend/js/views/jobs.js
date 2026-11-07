/*
 * Simulated jobs screen (Phase 4B.5).
 *
 * Create simulated jobs, list them, and inspect one in detail. The single most
 * important convention here: `POST /api/jobs` returns **HTTP 200 for a denied
 * job**. A denial is not a failed request - it is a recorded, terminal outcome
 * (`status === "denied"` with a `failure_reason`). This view therefore branches
 * on the response body, never on `response.ok`.
 *
 * Endpoints used - nothing else is called from this module:
 *   GET    /api/targets           (target-type options)
 *   GET    /api/target-instances  (optional association options)
 *   POST   /api/jobs              (create: JobCreateRequest)
 *   GET    /api/jobs              (JobSummary list; status/target/instance_id
 *                                  filters, sort, limit/offset pagination)
 *   GET    /api/jobs/{job_id}     (JobInfo detail)
 *   GET    /api/jobs/{job_id}/export
 *                                  (JobExportEnvelope: downloaded as a JSON
 *                                  file when the operator presses "Export
 *                                  JSON"; fetched on that click only)
 *
 * Verified schemas:
 *   JobCreateRequest {target: str (required), instance_id: str|null = null,
 *                     mode: str = "simulated", reference: str = ""}
 *   JobSummary       {job_id, status, created_at, target}
 *   JobInfo          JobSummary + {failure_reason, preview, instance_id,
 *                                  instance_target_type}
 *   JobExportEnvelope {schema_version: "1", exported_at: int,
 *                      simulation_only: true, job: JobInfo}
 *
 * SECURITY BOUNDARY
 * All network calls go through the shared client in js/api.js; this module
 * never calls fetch itself. All output is rendered through the textContent
 * helpers so no API string can become markup. Only the simulation mode is
 * offered. Nothing here triggers discovery, capability refresh or execution,
 * and nothing invents progress, worker activity or engine output. The export
 * is button-triggered, is written into a downloaded blob (the response body
 * is never rendered into the page), and its filename derives from the
 * server-minted job id only - never from the operator reference.
 */

import { api } from "../api.js";
import { registerView } from "../app.js";
import {
  Callout,
  InlineError,
  JobDetailView,
  NavLink,
  PageHead,
  Panel,
  RetryButton,
  SAFE_FAILURE_MESSAGE,
  StatRow,
  StatusBadge,
  SimulatedOnlyBadge,
} from "../components.js";
import { button, downloadBlob, empty, h, kv, pending, render, table, text, asArray, formatTimestamp } from "../dom.js";

/* ---- module state --------------------------------------------------------- */

/** Target types offered in the picker. @type {Array<object>} */
let targetTypes = [];
/** Target instances offered in the optional association picker. @type {Array<object>} */
let targetInstances = [];

let formState = { target: "", mode: "simulated", reference: "", instance_id: "" };

let listSlot = null;
let formSlot = null;
let formResultSlot = null;
let detailSlot = null;
let filterSlot = null;
/** Status slot under the detail panel's "Export JSON" button. */
let exportStatusSlot = null;
let detailSelectedJobId = "";
/**
 * The job whose details are actually painted in the detail slot (or the job
 * whose pending/error state is painted). Kept separately from the selection so
 * reconciliation can detect a panel that outlived its selection marker.
 */
let detailRenderedJobId = "";
/**
 * Monotonic generation for the detail-request lifecycle. A detail response is
 * applied only while it is still the newest request *and* the job it belongs
 * to is still selected. Clearing the selection (for example because a filter
 * or page change removed the job) therefore cannot be undone by a response
 * that was already in flight.
 */
let detailGeneration = 0;
/**
 * Job ids on the most recently applied list page. Selection is page-scoped:
 * details may only refer to a job the operator can currently see.
 * @type {Set<string>}
 */
let visibleJobIds = new Set();
/**
 * Monotonic generation for the list-request lifecycle. Every `renderList()`
 * call takes a fresh generation and applies its findings - data, loading and
 * error state alike - only while it is still the most recent one and while
 * the view still hangs off the slot it started on. An obsolete response can
 * therefore never overwrite a newer result. Module-local on purpose.
 */
let listGeneration = 0;
/**
 * Pagination + filtering for the list. `instanceFilter` is `""` (all),
 * `"none"` (jobs recorded with no instance) or a concrete instance id.
 */
let paginationState = {
  limit: 50,
  offset: 0,
  total: 0,
  statusFilter: "",
  targetFilter: "",
  instanceFilter: "",
  sort: "created_desc",
};

/* ---- helpers -------------------------------------------------------------- */

function isNonEmptyString(value) {
  return typeof value === "string" && value.length > 0;
}

/** True when any list filter narrows the result set. */
function hasActiveFilters() {
  return Boolean(
    paginationState.statusFilter ||
    paginationState.targetFilter ||
    paginationState.instanceFilter
  );
}

async function loadTargetOptions() {
  const res = await api.get("/api/targets");
  targetTypes = res.ok
    ? asArray(res.data && res.data.targets).filter(
        (t) => t !== null && typeof t === "object" && isNonEmptyString(t.target)
      )
    : [];
}

async function loadInstanceOptions() {
  const res = await api.get("/api/target-instances");
  targetInstances = res.ok
    ? asArray(res.data && res.data.instances).filter(
        (i) => i !== null && typeof i === "object" && isNonEmptyString(i.id)
      )
    : [];
}

/** Instances whose target_type matches `targetType`. */
function compatibleInstances(targetType) {
  return targetInstances.filter((i) => i.target_type === targetType);
}

/* ---- list filters --------------------------------------------------------- */

function filterSelect(id, label, value, options, onChange) {
  const select = h("select", {
    class: "field__control",
    attrs: [["id", id], ["name", id]],
    onChange,
    children: options.map((opt) =>
      h("option", {
        text: opt.label,
        attrs:
          opt.value === value
            ? [["value", opt.value], ["selected", ""]]
            : [["value", opt.value]],
      })
    ),
  });
  return h("div", {
    class: "filters__group",
    children: [
      h("label", { class: "field__label", text: label, attrs: [["for", id]] }),
      select,
    ],
  });
}

function renderFilters() {
  if (!filterSlot) {
    return;
  }
  const statuses = ["simulated", "denied", "failed"];
  const statusOptions = [{ label: "All statuses", value: "" }].concat(
    statuses.map((s) => ({ label: s, value: s }))
  );
  const targetOptions = [{ label: "All targets", value: "" }].concat(
    targetTypes.map((t) => ({ label: t.target, value: t.target }))
  );
  const instanceOptions = [
    { label: "All jobs", value: "" },
    { label: "No instance", value: "none" },
  ].concat(
    targetInstances.map((i) => ({
      label: (i.name || i.id) + " (" + i.target_type + ")",
      value: i.id,
    }))
  );
  const sortOptions = [
    { label: "Newest first", value: "created_desc" },
    { label: "Oldest first", value: "created_asc" },
  ];

  render(filterSlot, [
    h("div", {
      class: "toolbar",
      children: [
        h("p", { class: "toolbar__title", text: "Filter and sort" }),
        h("div", {
          class: "filters",
          children: [
            filterSelect(
              "jobs-filter-status",
              "Status",
              paginationState.statusFilter,
              statusOptions,
              (event) => {
                paginationState.statusFilter = event.target.value;
                paginationState.offset = 0;
                renderList();
              }
            ),
            filterSelect(
              "jobs-filter-target",
              "Target",
              paginationState.targetFilter,
              targetOptions,
              (event) => {
                paginationState.targetFilter = event.target.value;
                paginationState.offset = 0;
                renderList();
              }
            ),
            filterSelect(
              "jobs-filter-instance",
              "Instance",
              paginationState.instanceFilter,
              instanceOptions,
              (event) => {
                paginationState.instanceFilter = event.target.value;
                paginationState.offset = 0;
                renderList();
              }
            ),
            filterSelect(
              "jobs-filter-sort",
              "Sort",
              paginationState.sort,
              sortOptions,
              (event) => {
                paginationState.sort = event.target.value || "created_desc";
                paginationState.offset = 0;
                renderList();
              }
            ),
          ],
        }),
      ],
    }),
  ]);
}

/* ---- form ----------------------------------------------------------------- */

function onField(field) {
  return (event) => {
    formState[field] = event.target.value;
  };
}

function renderForm() {
  if (!formSlot) {
    return;
  }
  const compatible = compatibleInstances(formState.target);

  const targetSelect = h("select", {
    class: "field__control",
    attrs: [["id", "job-target"], ["name", "target"], ["required", ""]],
    onChange: (event) => {
      // Re-render so the instance picker reflects the new target's options.
      formState.target = event.target.value;
      renderForm();
    },
    children: [
      h("option", { text: "Select a target type...", attrs: [["value", ""]] }),
      ...asArray(targetTypes).map((t) =>
        h("option", {
          text: t.target,
          attrs:
            t.target === formState.target
              ? [["value", t.target], ["selected", ""]]
              : [["value", t.target]],
        })
      ),
    ],
  });

  const modeSelect = h("select", {
    class: "field__control",
    attrs: [["id", "job-mode"], ["name", "mode"]],
    onChange: (event) => {
      formState.mode = event.target.value;
    },
    children: [
      h("option", { text: "simulated", attrs: [["value", "simulated"], ["selected", ""]] }),
    ],
  });

  const referenceInput = h("input", {
    class: "field__control",
    attrs: [["id", "job-reference"], ["name", "reference"], ["maxlength", "256"]],
  });
  referenceInput.value = formState.reference || "";
  referenceInput.addEventListener("input", onField("reference"));

  const instanceSelect = h("select", {
    class: "field__control",
    attrs: [["id", "job-instance"], ["name", "instance_id"]],
    onChange: (event) => {
      formState.instance_id = event.target.value;
      renderForm();
    },
    children: [
      h("option", { text: "No target instance (optional)", attrs: [["value", ""]] }),
      ...asArray(targetInstances).map((inst) => {
        // Every instance stays visible; mismatches are labelled, never hidden.
        const label =
          inst.target_type === formState.target
            ? `${inst.name || inst.id} (${inst.target_type})`
            : `${inst.name || inst.id} (${inst.target_type} - not this target)`;
        return h("option", {
          text: label,
          attrs:
            inst.id === formState.instance_id
              ? [["value", inst.id], ["selected", ""]]
              : [["value", inst.id]],
        });
      }),
    ],
  });

  const mismatch =
    isNonEmptyString(formState.instance_id) &&
    isNonEmptyString(formState.target) &&
    !compatible.some((i) => i.id === formState.instance_id);

  const submit = h("button", {
    type: "button",
    class: "btn btn--primary",
    text: "Create simulated job",
    onClick: submitJob,
  });
  if (!isNonEmptyString(formState.target)) {
    submit.setAttribute("disabled", "");
  }

  render(formSlot, [
    h("div", {
      class: "form-grid",
      children: [
        h("div", {
          class: "field",
          children: [
            h("label", { class: "field__label", text: "Target type", attrs: [["for", "job-target"]] }),
            targetSelect,
            h("p", {
              class: "field__help",
              text: isNonEmptyString(formState.target)
                ? `${compatible.length} compatible target instance(s) for this target.`
                : "Required. Choose a target type.",
            }),
          ],
        }),
        h("div", {
          class: "field",
          children: [
            h("label", { class: "field__label", text: "Mode", attrs: [["for", "job-mode"]] }),
            modeSelect,
            h("p", {
              class: "field__help",
              text: "Only the simulation mode is offered - any other mode is always denied.",
            }),
          ],
        }),
        h("div", {
          class: "field",
          children: [
            h("label", {
              class: "field__label",
              text: "Target instance (optional)",
              attrs: [["for", "job-instance"]],
            }),
            instanceSelect,
            mismatch
              ? h("p", {
                  class: "field__help",
                  text:
                    "That target instance belongs to a different target type. The server will reject it - your selection has not been changed for you.",
                })
              : h("p", {
                  class: "field__help",
                  text: "Optional. The association is snapshotted onto the job when it is created.",
                }),
          ],
        }),
        h("div", {
          class: "field",
          children: [
            h("label", {
              class: "field__label",
              text: "Reference (optional)",
              attrs: [["for", "job-reference"]],
            }),
            referenceInput,
            h("p", { class: "field__help", text: "A label recorded with the job." }),
          ],
        }),
        h("div", {
          class: "actions",
          children: [submit, NavLink("Plan Preview", "/plan")],
        }),
      ],
    }),
  ]);
}

/* ---- create --------------------------------------------------------------- */

/** Guards the create form against double-submission while a request is pending. */
let submittingJob = false;

async function submitJob() {
  const target = formState.target;
  if (!isNonEmptyString(target)) {
    render(formResultSlot, [
      h("div", {
        class: "state state--error",
        attrs: [["role", "alert"]],
        children: [h("p", { class: "state__title", text: "Choose a target type first." })],
      }),
    ]);
    return;
  }
  if (submittingJob) {
    return;
  }
  submittingJob = true;
  try {
    // Exactly the JobCreateRequest keys: target required, the rest optional.
    // An omitted instance_id is not sent at all rather than sent as "".
    const payload = { target: target };
    if (isNonEmptyString(formState.instance_id)) {
      payload.instance_id = formState.instance_id;
    }
    payload.mode = formState.mode || "simulated";
    payload.reference = formState.reference || "";

    render(formResultSlot, [pending("Creating simulated job...")]);
    const res = await api.post("/api/jobs", payload);

    if (!res.ok) {
      // Domain errors (unknown_target, target_instance_not_found,
      // target_instance_type_mismatch) and Pydantic validation errors are both
      // already normalised to a single message by js/api.js.
      render(formResultSlot, [
        InlineError(res.error),
        RetryButton(submitJob, "Retry creating job"),
      ]);
      return;
    }

    const job = res.data;
    if (job === null || typeof job !== "object" || !isNonEmptyString(job.job_id)) {
      render(formResultSlot, [
        empty("No job returned", "The API answered without a usable job record."),
      ]);
      return;
    }

    render(formResultSlot, [createdSummary(job)]);
    // Refresh the list, then re-open the new job's detail so it is not lost.
    await renderList();
    await renderDetail(job.job_id);
  } catch {
    // A rejection here (never an HTTP error - those are handled above) must
    // not escape as an unhandled rejection, and its message must never reach
    // the DOM: only a fixed, safe sentence is shown.
    render(formResultSlot, [
      InlineError({ kind: "error", message: SAFE_FAILURE_MESSAGE }),
      RetryButton(submitJob, "Retry creating job"),
    ]);
  } finally {
    submittingJob = false;
  }
}

/**
 * A denied job is a successful *creation* with a denied outcome. Shown as such,
 * with the status and failure reason from the response - never as an error.
 */
function createdSummary(job) {
  const denied = job.status !== "simulated";
  const rows = [
    ["status", StatusBadge(job.status || "")],
    ["target", text(job.target || "-")],
    ["job id", h("code", { text: job.job_id })],
  ];
  if (job.failure_reason) {
    rows.push(["failure reason", StatusBadge(job.failure_reason)]);
  }
  return h("div", {
    class: denied ? "state state--error" : "state",
    attrs: [["role", "status"]],
    children: [
      h("p", {
        class: "state__title",
        text: denied ? "Job recorded with a denied outcome." : "Simulated job created.",
      }),
      h("p", {
        class: "panel__hint",
        text: denied
          ? "No engine ran and no plan was authorized. The recorded reason is shown below."
          : "No engine ran. This is a recorded simulation result.",
      }),
      kv(rows),
    ],
  });
}

/* ---- list ----------------------------------------------------------------- */

async function renderList() {
  if (!listSlot) {
    return;
  }
  // Take a fresh generation before issuing the request. Whatever answers
  // later, only the most recent issue may touch the slot or `paginationState`.
  const generation = ++listGeneration;
  const slot = listSlot;

  render(slot, pending("Loading jobs..."));
  const params = new URLSearchParams();
  if (paginationState.statusFilter) params.set("status", paginationState.statusFilter);
  if (paginationState.targetFilter) params.set("target", paginationState.targetFilter);
  if (paginationState.instanceFilter === "none") {
    // An explicit empty instance_id matches jobs with no association.
    params.set("instance_id", "");
  } else if (paginationState.instanceFilter) {
    params.set("instance_id", paginationState.instanceFilter);
  }
  params.set("sort", paginationState.sort || "created_desc");
  params.set("limit", String(paginationState.limit));
  params.set("offset", String(paginationState.offset));
  const qs = params.toString();
  const res = await api.get(qs ? "/api/jobs?" + qs : "/api/jobs");
  if (generation !== listGeneration || slot !== listSlot) {
    // A newer request superseded this one, or the view navigated away and a
    // fresh one was mounted: whatever this response holds, nobody may see it.
    return;
  }
  if (!res.ok) {
    render(slot, [InlineError(res.error), RetryButton(renderList)]);
    return;
  }
  const jobs = asArray(res.data && res.data.jobs).filter(
    (j) => j !== null && typeof j === "object" && isNonEmptyString(j.job_id)
  );
  const total = res.data && typeof res.data.total === "number" ? res.data.total : jobs.length;
  paginationState.total = total;
  // The selection is scoped to the page that was just rendered. If a filter,
  // sort or pagination change removed the selected job, its details must not
  // outlive the row (and its stale response must not repaint the panel).
  syncDetailWithVisibleJobs(jobs);

  if (jobs.length === 0 && paginationState.offset === 0) {
    render(slot, [
      hasActiveFilters()
        ? empty(
            "No jobs match your filters",
            "Adjust or clear the filters above to see recorded jobs again."
          )
        : empty("No jobs recorded yet", "Create a simulated job with the form above."),
    ]);
    return;
  }
  if (jobs.length === 0) {
    if (generation !== listGeneration || slot !== listSlot) {
      return;
    }
    paginationState.offset = 0;
    renderList();
    return;
  }

  const currentPage = Math.floor(paginationState.offset / paginationState.limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / paginationState.limit));

  const previous = button("Previous", () => {
    if (paginationState.offset > 0) {
      paginationState.offset = Math.max(0, paginationState.offset - paginationState.limit);
      renderList();
    }
  }, "btn");
  if (paginationState.offset === 0) {
    previous.setAttribute("disabled", "");
  }

  const next = button("Next", () => {
    if (paginationState.offset + paginationState.limit < total) {
      paginationState.offset = paginationState.offset + paginationState.limit;
      renderList();
    }
  }, "btn");
  if (paginationState.offset + paginationState.limit >= total) {
    next.setAttribute("disabled", "");
  }

  const paginationControls = h("div", {
    class: "pagination",
    children: [
      previous,
      h("span", { class: "pagination__info", text: `Page ${currentPage} of ${totalPages} (${total} total)` }),
      next,
    ],
  });

  // JobSummary only: `instance_id`, `failure_reason` and `preview` are
  // JobInfo-only and are deliberately NOT read from list rows.
  render(slot, [
    StatRow(
      [
        ["Matching jobs", total, undefined],
        ["On this page", jobs.length, undefined],
        ["Page", `${currentPage} of ${totalPages}`, undefined],
        ["Sort", paginationState.sort === "created_asc" ? "Oldest first" : "Newest first", undefined],
      ],
      "Job list summary"
    ),
    table(
      ["Job", "Status", "Target", "Created", "Details", "Delete"],
      jobs.map((job) => [
        h("code", { text: job.job_id }),
        StatusBadge(job.status || ""),
        text(job.target || "-"),
        formatTimestamp(job.created_at),
        button("Details", () => renderDetail(job.job_id)),
        button("Delete", () => confirmAndDeleteJob(job), "btn btn--danger"),
      ]),
      "Recorded simulated jobs"
    ),
    h("p", { class: "panel__hint", text: `${total} recorded job(s), latest first.` }),
    paginationControls,
  ]);
}

/* ---- detail --------------------------------------------------------------- */

/** The neutral detail state used both on mount and after a selection is cleared. */
function noJobSelectedState() {
  return empty(
    "No job selected",
    "Select a job from the current list to inspect its details."
  );
}

/**
 * Clear the selected job and invalidate any detail request still in flight.
 * The visible-id set is owned by the list response, so it is deliberately not
 * touched here.
 */
function clearSelectedDetail() {
  detailSelectedJobId = "";
  detailRenderedJobId = "";
  detailGeneration += 1;
  exportStatusSlot = null;
  if (detailSlot) {
    render(detailSlot, [noJobSelectedState()]);
  }
}

/**
 * Reconcile the detail panel with the page that was just applied. Called only
 * after a successful list response, so a transient request failure never
 * discards a selection that may still be valid.
 *
 * Both the selection marker and the job actually painted in the panel are
 * checked. They normally move together, but tracking the rendered identity
 * separately means a panel that somehow outlived a cleared selection marker
 * still cannot survive a list response that excludes it.
 */
function syncDetailWithVisibleJobs(jobs) {
  visibleJobIds = new Set(jobs.map((job) => job.job_id));
  const selected = isNonEmptyString(detailSelectedJobId) ? detailSelectedJobId : "";
  const rendered = isNonEmptyString(detailRenderedJobId) ? detailRenderedJobId : "";
  const selectedExcluded = selected !== "" && !visibleJobIds.has(selected);
  const renderedExcluded = rendered !== "" && !visibleJobIds.has(rendered);
  const panelWithoutSelection = selected === "" && rendered !== "";
  if (selectedExcluded || renderedExcluded || panelWithoutSelection) {
    clearSelectedDetail();
  }
}

async function renderDetail(jobId) {
  if (!detailSlot) {
    return;
  }
  if (!isNonEmptyString(jobId)) {
    clearSelectedDetail();
    return;
  }
  const generation = ++detailGeneration;
  detailSelectedJobId = jobId;
  detailRenderedJobId = jobId;
  render(detailSlot, [pending("Loading job details...")]);
  const res = await api.get(`/api/jobs/${jobId}`);
  if (generation !== detailGeneration || detailSelectedJobId !== jobId) {
    // A newer selection or a list reconciliation has superseded this request.
    return;
  }
  if (!res.ok) {
    // 404 job_not_found arrives as the shared object detail; describeError
    // renders it as "<message> (<code>)".
    render(detailSlot, [
      InlineError(res.error),
      RetryButton(() => renderDetail(jobId)),
    ]);
    return;
  }
  const job = res.data;
  if (job === null || typeof job !== "object" || !isNonEmptyString(job.job_id)) {
    detailSelectedJobId = "";
    detailRenderedJobId = "";
    render(detailSlot, [
      empty("No job returned", "The API answered without a usable job record."),
    ]);
    return;
  }
  // The response is authoritative for what is now painted.
  detailRenderedJobId = job.job_id;
  // A fresh status slot per render: the export controls never carry a stale
  // "downloaded"/error message over to a different job's detail.
  exportStatusSlot = h("div", { class: "section-body" });
  const note =
    job.status === "simulated"
      ? "This job recorded a simulation result. No engine was executed."
      : job.status === "denied"
        ? "This job was denied by the execution policy. No engine was executed."
        : "This job failed before a simulation result was recorded. No engine was executed.";
  const noteTitle =
    job.status === "simulated"
      ? "Simulated outcome recorded"
      : job.status === "denied"
        ? "Denied by the execution policy"
        : "Failed outcome recorded";
  const noteTone = job.status === "simulated" ? "ok" : job.status === "denied" ? "deny" : "warn";
  render(detailSlot, [
    h("div", {
      class: "detail-head",
      children: [
        SimulatedOnlyBadge(),
        h("p", { class: "panel__hint", text: `Recorded ${formatTimestamp(job.created_at)}` }),
      ],
    }),
    JobDetailView(job),
    Callout(noteTone, noteTitle, note, "status"),
    h("div", {
      class: "toolbar",
      children: [
        h("div", {
          class: "toolbar__group",
          children: [button("Export JSON", () => processExport(jobId))],
        }),
        exportStatusSlot,
      ],
    }),
  ]);
}

/* ---- export --------------------------------------------------------------- */

/**
 * In-flight guard: one export request at a time, mirroring `deletingJobId`.
 * A second click while one is open is ignored.
 * @type {string}
 */
let exportingJobId = "";

/**
 * Download one job's versioned JSON export envelope (M2.1 contract).
 *
 * Button-triggered only - nothing here polls or prefetches. The response body
 * becomes the downloaded file via `downloadBlob`; it is never rendered into
 * the page. The filename derives from the server-minted job id alone, exactly
 * like the server's `Content-Disposition`, so the operator reference can
 * never influence it.
 */
async function processExport(jobId) {
  const slot = exportStatusSlot;
  if (!slot) {
    return;
  }
  if (exportingJobId) {
    return;
  }
  exportingJobId = jobId;
  render(slot, [pending("Preparing the JSON export...")]);
  try {
    const res = await api.get(`/api/jobs/${jobId}/export`);
    // Fail closed: only a parseable body becomes a file. A 200 whose body is
    // not JSON (a proxy page, say) is an error, never a download.
    if (!res.ok || res.parseError || typeof res.raw !== "string") {
      render(slot, [
        InlineError(
          res.error || {
            kind: "http",
            message: "The export response was not readable JSON.",
            code: "",
            detail: null,
          }
        ),
        h("p", { class: "panel__hint", text: "The export was not generated." }),
      ]);
      return;
    }
    downloadBlob(
      new Blob([res.raw], { type: "application/json" }),
      `passorch-job-${jobId}.json`
    );
    render(slot, [
      h("p", {
        class: "panel__hint",
        text: "Export downloaded as a JSON file. The page itself was not changed.",
      }),
    ]);
  } catch {
    // A rejection outside the HTTP path (never an HTTP error - those are
    // handled above) must not escape unhandled, and its message must never
    // reach the DOM: only a fixed, safe sentence is shown.
    render(slot, [
      InlineError({ kind: "error", message: SAFE_FAILURE_MESSAGE }),
      h("p", { class: "panel__hint", text: "The export was not generated." }),
    ]);
  } finally {
    exportingJobId = "";
  }
}

/* ---- delete --------------------------------------------------------------- */

let deletingJobId = "";

function confirmAndDeleteJob(job) {
  const slot = listSlot;
  if (!slot) {
    return;
  }
  const label = job.target || job.job_id;
  render(slot, [
    h("div", {
      class: "state state--error",
      children: [
        h("p", {
          class: "state__title",
          text: `Delete job "${label}"?`,
        }),
        h("p", {
          class: "panel__hint",
          text: "This permanently removes the recorded job. This action cannot be undone.",
        }),
        h("div", {
          class: "actions",
          children: [
            button("Yes, delete", () => processDeleteJob(job.job_id, label), "btn btn--danger"),
            button("Cancel", renderList),
          ],
        }),
      ],
    }),
  ]);
}

async function processDeleteJob(jobId, label) {
  const slot = listSlot;
  if (!slot) {
    return;
  }
  if (deletingJobId) {
    return;
  }
  deletingJobId = jobId;
  render(slot, [pending(`Deleting job "${label}"...`)]);
  try {
    const res = await api.delete(`/api/jobs/${jobId}`);
    if (!res.ok) {
      deletingJobId = "";
      render(slot, [
        InlineError(res.error),
        RetryButton(renderList),
        h("p", { class: "panel__hint", text: "The job was not deleted." }),
      ]);
      return;
    }
    await renderList();
    if (detailSelectedJobId === jobId) {
      renderDetail("");
    }
  } catch {
    // Rejections only: an HTTP failure is handled above. Fixed sentence, no
    // exception text, and the list is refreshed so the screen is not left
    // showing a stale "Deleting..." state.
    render(slot, [
      InlineError({ kind: "error", message: SAFE_FAILURE_MESSAGE }),
      RetryButton(renderList),
      h("p", { class: "panel__hint", text: "The job was not deleted." }),
    ]);
  } finally {
    deletingJobId = "";
  }
}

/* ---- view ----------------------------------------------------------------- */

async function renderJobs(root) {
  // Reset the in-flight guards on entry: a request that was pending when the
  // user navigated away has detached the old slots, so its `finally` can no
  // longer be observed here. Leaving a guard latched would silently disable
  // this screen's forms for the rest of the session.
  submittingJob = false;
  exportingJobId = "";
  deletingJobId = "";

  listSlot = h("div", { class: "section-body" });
  formSlot = h("div", { class: "section-body" });
  formResultSlot = h("div", { class: "section-body" });
  detailSlot = h("div", { class: "section-body" });
  filterSlot = h("div", { class: "section-body" });

  formState = { target: "", mode: "simulated", reference: "", instance_id: "" };
  detailSelectedJobId = "";
  detailRenderedJobId = "";
  detailGeneration += 1;
  visibleJobIds = new Set();
  exportStatusSlot = null;
  paginationState = {
    limit: 50,
    offset: 0,
    total: 0,
    statusFilter: "",
    targetFilter: "",
    instanceFilter: "",
    sort: "created_desc",
  };

  render(root, [
    PageHead(
      "Jobs",
      "Create and inspect recorded simulated jobs. A denied job is a recorded outcome, not a request failure.",
      [NavLink("Plan Preview", "/plan"), NavLink("Instances", "/instances")]
    ),
    Panel(
      "Create a simulated job",
      [formSlot, formResultSlot],
      "Each job records what the policy authorised - and only ever a simulation."
    ),
    Panel(
      "Jobs",
      [
        filterSlot,
        listSlot,
        h("p", {
          class: "panel__hint",
          text:
            "A denied job is a recorded outcome, not an error: the policy refused to authorise a simulation. No engine binary is ever invoked, no attack is performed, and no progress, worker or engine output exists to show.",
        }),
      ],
      "Latest first. Details are loaded per job."
    ),
    Panel("Job details", detailSlot),
  ]);

  await Promise.all([loadTargetOptions(), loadInstanceOptions()]);
  renderFilters();
  renderForm();
  await renderList();
  render(detailSlot, [noJobSelectedState()]);
}

/* ---- registration --------------------------------------------------------- */

export const JobsView = registerView("/jobs", {
  title: "Jobs",
  render: (root) => {
    renderJobs(root).catch((err) => {
      render(root, [InlineError(err)]);
    });
  },
});

export default JobsView;
