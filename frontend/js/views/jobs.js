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
 *
 * Verified schemas:
 *   JobCreateRequest {target: str (required), instance_id: str|null = null,
 *                     mode: str = "simulated", reference: str = ""}
 *   JobSummary       {job_id, status, created_at, target}
 *   JobInfo          JobSummary + {failure_reason, preview, instance_id,
 *                                  instance_target_type}
 *
 * SECURITY BOUNDARY
 * All network calls go through the shared client in js/api.js; this module
 * never calls fetch itself. All output is rendered through the textContent
 * helpers so no API string can become markup. Only the simulation mode is
 * offered. Nothing here triggers discovery, capability refresh or execution,
 * and nothing invents progress, worker activity or engine output.
 */

import { api } from "../api.js";
import { registerView } from "../app.js";
import {
  InlineError,
  JobDetailView,
  NavLink,
  Panel,
  RetryButton,
  StatusBadge,
  SimulatedOnlyBadge,
  SimulationNotice,
} from "../components.js";
import { button, empty, h, kv, pending, render, table, text } from "../dom.js";

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
let detailSelectedJobId = "";
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

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

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
  render(listSlot, pending("Loading jobs..."));
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
  if (!res.ok) {
    render(listSlot, [InlineError(res.error), RetryButton(renderList)]);
    return;
  }
  const jobs = asArray(res.data && res.data.jobs).filter(
    (j) => j !== null && typeof j === "object" && isNonEmptyString(j.job_id)
  );
  const total = res.data && typeof res.data.total === "number" ? res.data.total : jobs.length;
  paginationState.total = total;

  if (jobs.length === 0 && paginationState.offset === 0) {
    render(listSlot, [
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
    paginationState.offset = 0;
    renderList();
    return;
  }

  const currentPage = Math.floor(paginationState.offset / paginationState.limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / paginationState.limit));

  const paginationControls = h("div", {
    class: "pagination",
    children: [
      button("Previous", () => {
        if (paginationState.offset > 0) {
          paginationState.offset = Math.max(0, paginationState.offset - paginationState.limit);
          renderList();
        }
      }, "btn"),
      h("span", { class: "pagination__info", text: `Page ${currentPage} of ${totalPages} (${total} total)` }),
      button("Next", () => {
        if (paginationState.offset + paginationState.limit < total) {
          paginationState.offset = paginationState.offset + paginationState.limit;
          renderList();
        }
      }, "btn"),
    ],
  });

  // JobSummary only: `instance_id`, `failure_reason` and `preview` are
  // JobInfo-only and are deliberately NOT read from list rows.
  render(listSlot, [
    table(
      ["Job", "Status", "Target", "Created", "", ""],
      jobs.map((job) => [
        h("code", { text: job.job_id }),
        StatusBadge(job.status || ""),
        text(job.target || "-"),
        job.created_at === undefined ? "-" : String(job.created_at),
        button("Details", () => renderDetail(job.job_id)),
        button("Delete", () => confirmAndDeleteJob(job)),
      ])
    ),
    h("p", { class: "panel__hint", text: `${total} recorded job(s), latest first.` }),
    paginationControls,
  ]);
}

/* ---- detail --------------------------------------------------------------- */

async function renderDetail(jobId) {
  if (!detailSlot) {
    return;
  }
  if (!isNonEmptyString(jobId)) {
    detailSelectedJobId = "";
    render(detailSlot, [empty("No job selected", "Pick a job from the list above.")]);
    return;
  }
  detailSelectedJobId = jobId;
  render(detailSlot, [pending("Loading job details...")]);
  const res = await api.get(`/api/jobs/${jobId}`);
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
    render(detailSlot, [
      empty("No job returned", "The API answered without a usable job record."),
    ]);
    return;
  }
  render(detailSlot, [
    h("div", { class: "section-body" }),
    SimulatedOnlyBadge(),
    JobDetailView(job),
    h("p", {
      class: "panel__hint",
      text:
        job.status === "simulated"
          ? "This job recorded a simulation result. No engine was executed."
          : "This job was denied by the execution policy. No engine was executed.",
    }),
  ]);
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
  } finally {
    deletingJobId = "";
  }
}

/* ---- view ----------------------------------------------------------------- */

async function renderJobs(root) {
  listSlot = h("div", { class: "section-body" });
  formSlot = h("div", { class: "section-body" });
  formResultSlot = h("div", { class: "section-body" });
  detailSlot = h("div", { class: "section-body" });
  filterSlot = h("div", { class: "section-body" });

  formState = { target: "", mode: "simulated", reference: "", instance_id: "" };
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
    SimulationNotice(),
    Panel(
      "Create a simulated job",
      [h("div", { class: "section-body" }), formSlot, formResultSlot],
      "Each job records what the policy authorised - and only ever a simulation."
    ),
    Panel("Jobs", [filterSlot, listSlot], "Latest first. Details are loaded per job."),
    Panel("Job details", detailSlot),
    Panel(
      "Simulation only",
      [
        h("p", {
          class: "panel__hint",
          text:
            "A denied job is a recorded outcome, not an error: it means the policy refused to authorise a simulation. No engine binary is ever invoked, no attack is performed, and no progress, worker or engine output exists to show.",
        }),
      ]
    ),
  ]);

  await Promise.all([loadTargetOptions(), loadInstanceOptions()]);
  renderFilters();
  renderForm();
  await renderList();
  render(detailSlot, [
    empty("No job selected", "Choose a job from the list above to see its recorded preview."),
  ]);
}

/* ---- registration --------------------------------------------------------- */

export const JobsView = registerView("/jobs", {
  title: "Jobs",
  render: (root) => {
    renderJobs(root).catch((err) => {
      render(root, [SimulationNotice(), InlineError(err)]);
    });
  },
});

export default JobsView;
