/*
 * Compatibility / plan-preview screen (Phase 4B.4).
 *
 * Requests a simulated plan preview for one target type. The single HTTP 200
 * path carries BOTH outcomes, so the screen must branch on
 * `authorization.authorized`, never on `response.ok`:
 *   authorized  -> plan + simulation are present;
 *   denied      -> plan === null and simulation === null, with a reason.
 *
 * Endpoints used - nothing else is called from this module:
 *   POST /api/targets/{target_id}/compatibility/plan  (mode, reference)
 *   GET  /api/targets                                 (target picker options)
 *
 * SECURITY BOUNDARY
 * All network calls go through the shared client in js/api.js; this module
 * never calls fetch itself. All output is rendered through the textContent
 * helpers in js/dom.js. `mode` is only ever one of the values the form offers
 * (simulated by default), and even an unsupported value is answered by the
 * policy with a structured denial rather than an execution. Nothing here
 * triggers discovery, capability refresh or engine execution.
 */

import { api } from "../api.js";
import { registerView } from "../app.js";
import {
  InlineError,
  NavLink,
  Panel,
  PlanPreviewView,
  RetryButton,
  SimulationNotice,
  SimulatedOnlyBadge,
  StatusBadge,
} from "../components.js";
import { button, empty, h, kv, pending, render, text } from "../dom.js";

/**
 * The modes the form offers. The API accepts any string, but this screen only
 * offers the simulation mode - anything else can only ever be denied.
 */
const MODES = ["simulated"];

/* ---- module state --------------------------------------------------------- */

let targetOptions = [];
let formState = { target: "", mode: "simulated", reference: "" };
let formSlot = null;
let resultSlot = null;

/* ---- helpers -------------------------------------------------------------- */

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.length > 0;
}

/**
 * Read the target the user arrived with (`#/plan?target=...`), as linked from
 * the Targets screen. The router already parsed it into `params.target`.
 */
function initialTarget(params) {
  if (params && isNonEmptyString(params.target)) {
    return params.target;
  }
  return "";
}

async function loadTargetOptions() {
  const res = await api.get("/api/targets");
  if (!res.ok) {
    targetOptions = [];
    return;
  }
  targetOptions = asArray(res.data && res.data.targets).filter(
    (t) => t !== null && typeof t === "object" && isNonEmptyString(t.target)
  );
}

function renderForm() {
  if (!formSlot) {
    return;
  }
  const options = targetOptions;

  const targetSelect = h("select", {
    class: "field__control",
    attrs: [["id", "plan-target"], ["name", "target"], ["required", ""]],
    onChange: (event) => {
      // Re-render so the submit button enables once a target is chosen - the
      // same pattern the jobs form uses (the disabled attribute is set at
      // render time and a bare state update would leave the button unusable
      // when the view was opened directly at #/plan without a preselected
      // target from the Targets screen).
      formState.target = event.target.value;
      renderForm();
    },
    children: [
      h("option", { text: "Select a target type...", attrs: [["value", ""]] }),
      ...options.map((option) =>
        h("option", {
          text: option.target,
          attrs:
            option.target === formState.target
              ? [["value", option.target], ["selected", ""]]
              : [["value", option.target]],
        })
      ),
    ],
  });

  const modeSelect = h("select", {
    class: "field__control",
    attrs: [["id", "plan-mode"], ["name", "mode"]],
    onChange: (event) => {
      formState.mode = event.target.value;
    },
    children: MODES.map((mode) =>
      h("option", {
        text: mode,
        attrs: mode === formState.mode ? [["value", mode], ["selected", ""]] : [["value", mode]],
      })
    ),
  });

  const referenceInput = h("input", {
    class: "field__control",
    attrs: [["id", "plan-reference"], ["name", "reference"], ["maxlength", "256"]],
  });
  referenceInput.value = formState.reference || "";
  referenceInput.addEventListener("input", (event) => {
    formState.reference = event.target.value;
  });

  const submit = button(
    "Preview simulated plan",
    submitPreview,
    "btn btn--primary"
  );
  if (!isNonEmptyString(formState.target)) {
    submit.setAttribute("disabled", "");
  }

  const grid = h("div", {
    class: "form-grid",
    children: [
      h("div", {
        class: "field",
        children: [
          h("label", {
            class: "field__label",
            text: "Target type",
            attrs: [["for", "plan-target"]],
          }),
          targetSelect,
          h("p", {
            class: "field__help",
            text:
              options.length === 0
                ? "No target types are available yet."
                : "The target types the platform can plan against.",
          }),
        ],
      }),
      h("div", {
        class: "field",
        children: [
          h("label", { class: "field__label", text: "Mode", attrs: [["for", "plan-mode"]] }),
          modeSelect,
          h("p", {
            class: "field__help",
            text: "Only the simulation mode is offered - no other mode can be authorised.",
          }),
        ],
      }),
      h("div", {
        class: "field",
        children: [
          h("label", {
            class: "field__label",
            text: "Reference (optional)",
            attrs: [["for", "plan-reference"]],
          }),
          referenceInput,
          h("p", { class: "field__help", text: "A label recorded with the request, if you want one." }),
        ],
      }),
      h("div", {
        class: "actions",
        children: [submit, NavLink("Back to Targets", "/targets")],
      }),
    ],
  });

  render(formSlot, [grid]);
}

/* ---- preview -------------------------------------------------------------- */

function renderResult(preview) {
  render(resultSlot, [
    h("div", { class: "section-body" }),
    // Branch on the body, never on response.ok: a denial is HTTP 200.
    preview.authorization &&
    preview.authorization.authorized === true ? (
      authorizedSummary(preview)
    ) : (
      deniedSummary(preview)
    ),
    PlanPreviewView(preview),
  ]);
}

function authorizedSummary(preview) {
  return h("div", {
    class: "state",
    attrs: [["role", "status"]],
    children: [
      h("p", {
        class: "state__title",
        text: "Authorized - this is what a simulated run would look like.",
      }),
      h("p", {
        class: "panel__hint",
        text: "No engine ran. The steps below are the plan a simulation would follow, plus its simulated result.",
      }),
      kv([
        ["target", text(preview.target || "-")],
        ["adapter", text((preview.plan && preview.plan.adapter) || "-")],
        ["simulation", StatusBadge((preview.simulation && preview.simulation.status) || "")],
      ]),
    ],
  });
}

function deniedSummary(preview) {
  const auth = preview.authorization || {};
  const rows = [
    ["authorized", h("span", { children: [SimulatedOnlyBadge()] })],
    ["reason", StatusBadge(auth.reason || "")],
  ];
  if (auth.detail) {
    rows.push(["detail", text(auth.detail)]);
  }
  return h("div", {
    class: "state state--error",
    attrs: [["role", "alert"]],
    children: [
      h("p", { class: "state__title", text: "Denied - no simulation was produced." }),
      h("p", {
        class: "panel__hint",
        text: "The execution policy refused this request. No engine was started, and no plan or result exists.",
      }),
      kv(rows),
    ],
  });
}

async function submitPreview() {
  const target = formState.target;
  if (!isNonEmptyString(target)) {
    render(resultSlot, [
      h("div", {
        class: "state state--error",
        attrs: [["role", "alert"]],
        children: [h("p", { class: "state__title", text: "Choose a target type first." })],
      }),
    ]);
    return;
  }
  const payload = {
    mode: formState.mode || "simulated",
    reference: formState.reference || "",
  };
  render(resultSlot, [pending("Requesting plan preview...")]);

  const res = await api.post(`/api/targets/${target}/compatibility/plan`, payload);
  if (!res.ok) {
    render(resultSlot, [
      InlineError(res.error),
      RetryButton(submitPreview, "Retry preview"),
      h("p", {
        class: "panel__hint",
        text: "The preview request failed before the policy could evaluate it.",
      }),
    ]);
    return;
  }
  const preview = res.data;
  if (preview === null || typeof preview !== "object") {
    render(resultSlot, [
      empty("No preview returned", "The API answered without a usable preview body."),
    ]);
    return;
  }
  renderResult(preview);
}

/* ---- view ----------------------------------------------------------------- */

async function renderPlan(root, params) {
  formSlot = h("div", { class: "section-body" });
  resultSlot = h("div", { class: "section-body" });

  formState = {
    target: initialTarget(params),
    mode: "simulated",
    reference: "",
  };

  render(root, [
    SimulationNotice(),
    Panel(
      "Compatibility plan preview",
      [
        h("div", { class: "section-body" }),
        formSlot,
        resultSlot,
      ],
      "Request a simulated plan preview. Nothing is ever executed."
    ),
  ]);

  await loadTargetOptions();
  // Keep an incoming target if it exists in the catalog, otherwise clear it so
  // the user picks deliberately.
  if (formState.target && !targetOptions.some((o) => o.target === formState.target)) {
    formState.target = "";
  }
  renderForm();

  // An arriving target is a request for a preview, not just a pre-filled form.
  if (formState.target) {
    await submitPreview();
  } else {
    render(resultSlot, [
      empty(
        "No preview yet",
        "Pick a target type and request a preview. A denied preview still returns HTTP 200 - this screen always shows the reason."
      ),
    ]);
  }
}

/* ---- registration --------------------------------------------------------- */

export const PlanView = registerView("/plan", {
  title: "Plan Preview",
  render: (root, params) => {
    renderPlan(root, params || {}).catch((err) => {
      render(root, [SimulationNotice(), InlineError(err)]);
    });
  },
});

export default PlanView;
