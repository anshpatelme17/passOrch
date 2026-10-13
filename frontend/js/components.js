/*
 * Reusable UI components.
 *
 * Everything here composes the safe helpers from js/dom.js. The components
 * encode the response conventions the backend actually uses, so views do not
 * repeat them:
 *
 *   1. A denied compatibility plan is HTTP 200 with authorization.authorized
 *      === false, plan === null and simulation === null. It must be shown as a
 *      structured denial, never as a network failure.
 *   2. A job carries instance_id and instance_target_type as a snapshot; both
 *      are "" when the job was created without an association.
 */

import { badge, h, kv, table, text } from "./dom.js";

/* ---- status tones ------------------------------------------------------- */

const TONES = {
  simulated: "ok",
  authorized: "ok",
  selected: "ok",
  verified: "ok",
  denied: "deny",
  no_verified_capability: "deny",
  unsupported_execution_mode: "deny",
  non_simulation_request: "deny",
  invalid_request: "deny",
  unknown: "warn",
  error: "deny",
  pending: "warn",
};

/** Map a backend status string to a semantic tone, defaulting to muted. */
export function toneFor(value) {
  if (typeof value !== "string") {
    return "muted";
  }
  return TONES[value] || "muted";
}

/* ---- sections and links -------------------------------------------------- */

/**
 * A titled panel whose body is either content or a state helper result.
 * `body` may be a Node, an array of Nodes, or falsy (rendered as an empty area).
 */
export function Section(title, body, hint) {
  const kids = [];
  const head = h("div", { class: "panel__head" });
  head.appendChild(h("h2", { class: "panel__title", text: title }));
  if (hint) {
    head.appendChild(h("p", { class: "panel__hint", text: hint }));
  }
  kids.push(head);

  const bodies = Array.isArray(body) ? body : [body];
  for (const child of bodies) {
    if (child instanceof Node) {
      kids.push(child);
    }
  }
  return h("section", { class: "panel", children: kids });
}

/** An internal link that drives the existing hash router. */
export function NavLink(label, path) {
  return h("a", {
    class: "btn",
    attrs: [["href", `#${path}`]],
    text: label,
  });
}

/**
 * A retry button. `onClick` must re-run the failed request and re-render.
 */
export function RetryButton(onClick, label) {
  return h("button", {
    type: "button",
    class: "btn",
    text: label || "Retry",
    onClick,
  });
}

/** Inline error text used inside a section that still rendered other content. */
export function InlineError(error) {
  const err =
    error && typeof error === "object" && "kind" in error
      ? error
      : { kind: "error", message: String((error && error.message) || error || "unknown error") };
  const kids = [h("p", { class: "state__title", text: "This section failed to load" })];
  kids.push(h("p", { class: "state__hint", text: err.message || "" }));
  return h("div", {
    class: "state state--error",
    attrs: [["role", "alert"]],
    children: kids,
  });
}

/* ---- small pieces ------------------------------------------------------- */

export function StatusBadge(value) {
  return badge(value, toneFor(value));
}

export function SimulatedOnlyBadge() {
  return badge("simulated only", "warn");
}

export function BoolBadge(value, trueLabel, falseLabel) {
  const label = value ? trueLabel : falseLabel;
  return badge(label, value ? "ok" : "muted");
}

export function TextOrDash(value) {
  return h("span", { text: value === undefined || value === null || value === "" ? "-" : value });
}

export function CodeOrDash(value) {
  if (value === undefined || value === null || value === "") {
    return h("span", { class: "badge badge--muted", text: "-" });
  }
  return h("code", { text: value });
}

export function StepsList(steps) {
  const ul = h("ul", { class: "list-plain" });
  if (!Array.isArray(steps) || steps.length === 0) {
    ul.appendChild(h("li", { text: "none" }));
  } else {
    for (const step of steps) {
      ul.appendChild(h("li", { text: step }));
    }
  }
  return ul;
}

/* ---- simulation-only notice --------------------------------------------- */

/**
 * The persistent banner. Rendered by the shell, and also exposed as a function
 * so any view can restate it next to a plan or job result.
 */
export function SimulationNotice() {
  return h("p", {
    class: "simulation-notice",
    attrs: [["role", "status"], ["data-simulation-notice", "true"]],
    text: "Simulation only - no engine is ever executed.",
  });
}

/* ---- panels ------------------------------------------------------------- */

export function Panel(title, children, hint) {
  const kids = [];
  const head = h("div", { class: "panel__head" });
  head.appendChild(h("h2", { class: "panel__title", text: title }));
  if (hint) {
    head.appendChild(h("p", { class: "panel__hint", text: hint }));
  }
  kids.push(head);
  // Accept a single node or an array, matching h() and render() in dom.js.
  const body = Array.isArray(children) ? children : [children];
  for (const child of body) {
    if (child instanceof Node) {
      kids.push(child);
    } else if (child !== null && child !== undefined) {
      kids.push(h("p", { text: child }));
    }
  }
  return h("section", { class: "panel", children: kids });
}

/* ---- plan rendering ----------------------------------------------------- */

function SelectionBlock(preview) {
  const sel = preview.selection || {};
  const candidates = Array.isArray(sel.candidates) ? sel.candidates : [];
  return h("section", {
    class: "panel",
    children: [
      h("h3", { text: "Selection" }),
      kv([
        ["status", StatusBadge(sel.status || "")],
        ["reason", text(sel.reason || "-")],
        ["selected engine", text(sel.selected_engine || "-")],
        ["candidates", StepsList(candidates.length ? candidates : null)],
      ]),
    ],
  });
}

function AuthorizationBlock(preview) {
  const auth = preview.authorization || {};
  const rows = [
    ["authorized", BoolBadge(auth.authorized === true, "yes", "no")],
    ["status", StatusBadge(auth.status || "")],
    ["reason", text(auth.reason || "-")],
  ];
  if (auth.detail) {
    rows.push(["detail", text(auth.detail)]);
  }
  rows.push(["mode", text(auth.mode || "-")]);
  return h("section", {
    class: "panel",
    children: [h("h3", { text: "Authorization" }), kv(rows)],
  });
}

function RequestBlock(preview) {
  const req = preview.request || {};
  const matched = Array.isArray(req.matched_formats) ? req.matched_formats : [];
  return h("section", {
    class: "panel",
    children: [
      h("h3", { text: "Request" }),
      kv([
        ["mode", text(req.mode || "-")],
        ["engine", text(req.engine || "-")],
        ["selection status", StatusBadge(req.selection_status || "")],
        ["capability status", StatusBadge(req.capability_status || "")],
        ["matched formats", StepsList(matched.length ? matched : null)],
        ["reference", text(req.reference || "-")],
      ]),
    ],
  });
}

/**
 * Render a plan preview. Handles both outcomes of the single HTTP 200 path:
 * authorised (plan + simulation present) and denied (both null).
 */
export function PlanPreviewView(preview) {
  if (!preview || typeof preview !== "object") {
    return h("div", { class: "state state--empty", text: "No plan preview available." });
  }

  const wrap = h("div", { class: "stack" });

  const head = h("div", { class: "panel__head" });
  head.appendChild(h("h2", { class: "panel__title", text: `Plan preview: ${preview.target || "-"}` }));
  head.appendChild(SimulatedOnlyBadge());
  wrap.appendChild(h("section", { class: "panel", children: [head] }));

  wrap.appendChild(RequestBlock(preview));
  wrap.appendChild(SelectionBlock(preview));
  wrap.appendChild(AuthorizationBlock(preview));

  // A denied preview has plan === null and simulation === null.
  const plan = preview.plan;
  const planRows = plan
    ? [
        ["adapter", text(plan.adapter || "-")],
        ["mode", text(plan.mode || "-")],
        ["capability status", StatusBadge(plan.capability_status || "")],
        ["selected engine", text(plan.selected_engine || "-")],
        ["simulated", BoolBadge(plan.simulated === true, "yes", "no")],
        ["steps", StepsList(plan.steps)],
      ]
    : [["plan", text("none (denied)")]];
  wrap.appendChild(
    h("section", {
      class: "panel",
      children: [h("h3", { text: "Simulation plan" }), kv(planRows)],
    })
  );

  const sim = preview.simulation;
  if (sim) {
    wrap.appendChild(
      h("section", {
        class: "panel",
        children: [
          h("h3", { text: "Simulation result" }),
          kv([
            ["status", StatusBadge(sim.status || "")],
            ["simulation id", CodeOrDash(sim.simulation_id)],
            ["adapter", text(sim.adapter || "-")],
            ["engine", text(sim.engine || "-")],
            ["message", text(sim.message || "-")],
            ["steps", StepsList(sim.steps)],
          ]),
        ],
      })
    );
  } else {
    wrap.appendChild(
      h("section", {
        class: "panel",
        children: [
          h("h3", { text: "Simulation result" }),
          h("p", {
            class: "panel__hint",
            text: "No simulation was produced for this request.",
          }),
        ],
      })
    );
  }

  return wrap;
}

/* ---- jobs --------------------------------------------------------------- */

function AssociationBlock(job) {
  const id = job.instance_id || "";
  const type = job.instance_target_type || "";
  if (!id && !type) {
    return h("p", {
      class: "panel__hint",
      text: "Created without a target-instance association.",
    });
  }
  return kv([
    ["instance id", CodeOrDash(id)],
    ["instance target type", StatusBadge(type || "")],
  ]);
}

/** Full job detail card, including the association snapshot. */
export function JobDetailView(job) {
  const wrap = h("div", { class: "stack" });

  const head = h("div", { class: "panel__head" });
  head.appendChild(h("h2", { class: "panel__title", text: `Job ${job.job_id || "-"}` }));
  head.appendChild(StatusBadge(job.status || ""));
  wrap.appendChild(h("section", { class: "panel", children: [head] }));

  const summaryRows = [
    ["status", StatusBadge(job.status || "")],
    ["target", text(job.target || "-")],
    ["created", text(job.created_at === undefined ? "-" : String(job.created_at))],
    ["job id", CodeOrDash(job.job_id)],
  ];
  if (job.failure_reason) {
    summaryRows.push(["failure reason", text(job.failure_reason)]);
  }
  wrap.appendChild(
    h("section", { class: "panel", children: [h("h3", { text: "Summary" }), kv(summaryRows)] })
  );

  wrap.appendChild(
    h("section", {
      class: "panel",
      children: [
        h("h3", { text: "Target instance association" }),
        AssociationBlock(job),
      ],
    })
  );

  if (job.preview) {
    wrap.appendChild(PlanPreviewView(job.preview));
  } else {
    wrap.appendChild(
      h("p", { class: "panel__hint", text: "No plan preview was recorded for this job." })
    );
  }

  return wrap;
}

/**
 * Summary row for the job history table.
 *
 * Reads **only** JobSummary fields (`job_id`, `status`, `created_at`, `target`).
 * The instance association is a JobInfo-only field and therefore absent from
 * `GET /api/jobs` rows, so it must not be rendered here - it is shown in the
 * job detail view instead.
 */
export function JobRow(job) {
  return [
    CodeOrDash(job.job_id),
    StatusBadge(job.status || ""),
    text(job.target || "-"),
    job.created_at === undefined ? "-" : String(job.created_at),
  ];
}

export function JobTable(jobs) {
  return table(["Job", "Status", "Target", "Created"], jobs.map(JobRow));
}

/* ---- engines / targets / instances -------------------------------------- */

export function EnginesTable(engines) {
  return table(
    ["Engine", "Discovered", "Capability", "Formats", "Real-engine compatible"],
    engines.map((e) => [
      text(e.engine || "-"),
      BoolBadge(e.discovered === true, "yes", "no"),
      StatusBadge(e.capability_status || ""),
      text((e.formats || []).join(", ") || "-"),
      BoolBadge(e.real_engine_compatible === true, "yes", "no"),
    ])
  );
}

export function TargetsTable(targets) {
  return table(
    ["Target", "Description", "Formats", "Enabled", "Usable"],
    targets.map((t) => [
      text(t.target || "-"),
      text(t.description || "-"),
      text((t.formats || []).join(", ") || "-"),
      BoolBadge(t.enabled === true, "yes", "no"),
      BoolBadge(t.usable === true, "yes", "no"),
    ])
  );
}

/**
 * Instance table. `onSelect` is optional; when omitted the instance name
 * becomes a plain button instead of a row action.
 */
export function InstancesTable(instances, onSelect) {
  return table(
    ["Name", "Id", "Target type", "Description", "Created"],
    instances.map((inst) => [
      onSelect
        ? h("button", {
            type: "button",
            class: "btn",
            text: inst.name || inst.id,
            onClick: () => onSelect(inst),
          })
        : text(inst.name || "-"),
      CodeOrDash(inst.id),
      StatusBadge(inst.target_type || ""),
      text(inst.description || "-"),
      inst.created_at === undefined ? "-" : String(inst.created_at),
    ])
  );
}

/** Target picker used by the plan-preview and job-creation screens. */
export function TargetSelect(targets, selected, onChange) {
  const select = h("select", {
    class: "field__control",
    attrs: [["name", "target"]],
    onChange: (ev) => onChange(ev.target.value),
  });
  for (const t of targets) {
    const value = t.target || "";
    const option = h("option", { attrs: [["value", value]] });
    option.textContent = value;
    option.selected = value === selected;
    select.appendChild(option);
  }
  return select;
}
