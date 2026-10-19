/*
 * Target instances screen (Phase 4B.3).
 *
 * Read + create + delete over the Phase 3C target-instance endpoints. Every
 * mutation requires an explicit confirmation before the request is issued, and
 * the list is refreshed only after a *successful* mutation - never on failure.
 *
 * Endpoints used - nothing else is called from this module:
 *   GET    /api/target-instances       -> {instances: [...], count, total, ...}
 *                                          (optional target_type / name filters,
 *                                          limit / offset pagination)
 *   POST   /api/target-instances       -> 201 TargetInstance
 *   DELETE /api/target-instances/{id}  -> 204 No Content (empty body)
 *   GET    /api/target-instances/{id}  -> 200 TargetInstance (detail row)
 *
 * Schemas (verified against app/api/schemas.py):
 *   TargetInstanceCreateRequest: {name: str, target_type: str, description: str = ""}
 *   TargetInstance: {id, name, target_type, description, created_at}
 * Server constraints mirrored in client-side validation:
 *   name non-blank and <= 128 chars; description <= 1024 chars;
 *   target_type must be a mapped type from GET /api/targets.
 *
 * SECURITY BOUNDARY
 * All network calls go through the shared client in js/api.js; this module
 * never calls fetch itself. All output is rendered through the textContent
 * helpers in js/dom.js, so no API string (including a user-typed name or
 * description) can ever become markup. DELETE answers 204 with no body, which
 * the shared client tolerates. Nothing here triggers discovery, capability
 * refresh or engine execution.
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
import { badge, button, empty, h, pending, render, table, text } from "../dom.js";

/* ---- limits mirrored from app/engines/target_instances.py ----------------- */

const MAX_NAME_LENGTH = 128;
const MAX_DESCRIPTION_LENGTH = 1024;

/* ---- module state --------------------------------------------------------- */

/**
 * Options for the `target_type` picker, populated from GET /api/targets so the
 * client never hardcodes or invents a catalog of its own.
 * @type {Array<{target: string, usable: boolean}>}
 */
let targetTypes = [];

/** The client-side form values, cleared after a successful create. */
let formState = { name: "", target_type: "", description: "" };

/** Slots that must be re-rendered when the list or options change. */
let listSlot = null;
let formSlot = null;
let feedbackSlot = null;
let filterSlot = null;
/** `targetTypeFilter` is an exact target type; `nameFilter` a substring. */
let paginationState = {
  limit: 50,
  offset: 0,
  total: 0,
  targetTypeFilter: "",
  nameFilter: "",
};

/* ---- helpers -------------------------------------------------------------- */

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

/**
 * Client-side validation mirroring the server constraints. Returns a list of
 * human-readable problems; empty means the request may be issued.
 */
function validateForm(state) {
  const problems = [];
  if (!isNonEmptyString(state && state.name)) {
    problems.push("Name is required.");
  } else if (state.name.length > MAX_NAME_LENGTH) {
    problems.push(`Name must be at most ${MAX_NAME_LENGTH} characters.`);
  }
  if (!isNonEmptyString(state && state.target_type)) {
    problems.push("A target type is required.");
  }
  const description = state && typeof state.description === "string" ? state.description : "";
  if (description.length > MAX_DESCRIPTION_LENGTH) {
    problems.push(`Description must be at most ${MAX_DESCRIPTION_LENGTH} characters.`);
  }
  return problems;
}

/* ---- feedback ------------------------------------------------------------- */

function renderFeedback(kind, message) {
  if (!feedbackSlot) {
    return;
  }
  render(feedbackSlot, []);
  if (!kind) {
    return;
  }
  if (kind === "error") {
    render(feedbackSlot, [
      h("div", {
        class: "state state--error",
        attrs: [["role", "alert"]],
        children: [h("p", { class: "state__title", text: message })],
      }),
    ]);
    return;
  }
  render(feedbackSlot, [
    h("div", {
      class: "state",
      attrs: [["role", "status"]],
      children: [h("p", { class: "state__title", text: message })],
    }),
  ]);
}

/* ---- list filters --------------------------------------------------------- */

function renderFilters() {
  if (!filterSlot) {
    return;
  }
  const typeNodes = [h("option", { text: "All types", attrs: [["value", ""]] })];
  for (const option of targetTypes) {
    const isSelected = option.target === paginationState.targetTypeFilter;
    typeNodes.push(
      h("option", {
        text: option.target,
        attrs: isSelected
          ? [["value", option.target], ["selected", ""]]
          : [["value", option.target]],
      })
    );
  }
  const typeSelect = h("select", {
    class: "field__control",
    attrs: [["id", "instances-filter-type"], ["name", "target_type"]],
    onChange: (event) => {
      paginationState.targetTypeFilter = event.target.value;
      paginationState.offset = 0;
      renderList();
    },
    children: typeNodes,
  });

  const nameInput = h("input", {
    class: "field__control",
    attrs: [
      ["id", "instances-filter-name"],
      ["name", "name"],
      ["maxlength", String(MAX_NAME_LENGTH)],
    ],
  });
  nameInput.value = paginationState.nameFilter || "";

  render(filterSlot, [
    h("div", {
      class: "filters",
      children: [
        h("div", {
          class: "filters__group",
          children: [
            h("label", {
              class: "field__label",
              text: "Target type",
              attrs: [["for", "instances-filter-type"]],
            }),
            typeSelect,
          ],
        }),
        h("div", {
          class: "filters__group",
          children: [
            h("label", {
              class: "field__label",
              text: "Name contains",
              attrs: [["for", "instances-filter-name"]],
            }),
            nameInput,
          ],
        }),
        h("div", {
          class: "filters__group",
          children: [
            button(
              "Apply filters",
              () => {
                paginationState.nameFilter = nameInput.value || "";
                paginationState.offset = 0;
                renderList();
              },
              "btn btn--primary"
            ),
            button("Clear", () => {
              paginationState.nameFilter = "";
              paginationState.targetTypeFilter = "";
              paginationState.offset = 0;
              renderFilters();
              renderList();
            }, "btn"),
          ],
        }),
      ],
    }),
  ]);
}

/* ---- list ----------------------------------------------------------------- */

async function renderList() {
  if (!listSlot) {
    return;
  }
  render(listSlot, pending("Loading target instances..."));
  const params = new URLSearchParams();
  if (paginationState.targetTypeFilter) {
    params.set("target_type", paginationState.targetTypeFilter);
  }
  if (paginationState.nameFilter) {
    params.set("name", paginationState.nameFilter);
  }
  params.set("limit", String(paginationState.limit));
  params.set("offset", String(paginationState.offset));
  const qs = params.toString();
  const res = await api.get(qs ? "/api/target-instances?" + qs : "/api/target-instances");
  if (!res.ok) {
    render(listSlot, [InlineError(res.error), RetryButton(renderList)]);
    return;
  }
  const instances = asArray(res.data && res.data.instances).filter(
    (i) => i !== null && typeof i === "object" && isNonEmptyString(i.id)
  );
  const total = res.data && typeof res.data.total === "number" ? res.data.total : instances.length;
  paginationState.total = total;

  if (instances.length === 0 && paginationState.offset === 0) {
    const filtered = Boolean(paginationState.targetTypeFilter || paginationState.nameFilter);
    render(listSlot, [
      filtered
        ? empty(
            "No target instances match your filters",
            "Adjust or clear the filters above to see instances again."
          )
        : empty(
            "No target instances yet",
            "Create one below to give a specific thing a name that simulated jobs can be associated with."
          ),
    ]);
    return;
  }
  if (instances.length === 0) {
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

  render(listSlot, [
    table(
      ["Name", "Id", "Target type", "Description", "Created", "Actions"],
      instances.map((inst) => [
        text(inst.name || "-"),
        h("code", { text: inst.id }),
        badge(inst.target_type || "", "muted"),
        text(inst.description || "-"),
        typeof inst.created_at === "number" ? String(inst.created_at) : "-",
        button("Delete", () => confirmAndDelete(inst)),
      ])
    ),
    h("p", { class: "panel__hint", text: `${total} target instance(s) defined.` }),
    paginationControls,
  ]);
}

/* ---- target-type options -------------------------------------------------- */

async function loadTargetTypes() {
  // The type list comes from the same catalog the Targets screen reads, so the
  // form can never offer a type the server would reject as unmapped.
  const res = await api.get("/api/targets");
  if (!res.ok) {
    targetTypes = [];
  } else {
    targetTypes = asArray(res.data && res.data.targets).filter(
      (t) => t !== null && typeof t === "object" && isNonEmptyString(t.target)
    );
  }
}

/* ---- create form ---------------------------------------------------------- */

function onField(field) {
  return (event) => {
    formState[field] = event.target.value;
  };
}

function renderForm() {
  if (!formSlot) {
    return;
  }
  const options = asArray(targetTypes);

  const nameInput = h("input", {
    class: "field__control",
    attrs: [
      ["id", "instance-name"],
      ["name", "name"],
      ["maxlength", String(MAX_NAME_LENGTH)],
      ["required", ""],
    ],
  });
  nameInput.value = formState.name || "";
  nameInput.addEventListener("input", onField("name"));

  const descInput = h("input", {
    class: "field__control",
    attrs: [
      ["id", "instance-description"],
      ["name", "description"],
      ["maxlength", String(MAX_DESCRIPTION_LENGTH)],
    ],
  });
  descInput.value = formState.description || "";
  descInput.addEventListener("input", onField("description"));

  const submit = button("Create target instance", submitForm, "btn btn--primary");
  if (options.length === 0) {
    submit.setAttribute("disabled", "");
  }

  // Options are built with h() (including `selected` as an attribute) so the
  // select is assembled through the shared helper rather than direct DOM calls.
  let typeOptionsNodes = [
    h("option", { text: "Select a target type...", attrs: [["value", ""]] }),
  ];
  if (options.length === 0) {
    typeOptionsNodes = [h("option", { text: "No target types available", attrs: [["value", ""]] })];
  } else {
    for (const option of options) {
      const isSelected = option.target === formState.target_type;
      typeOptionsNodes.push(
        h("option", {
          text: option.target,
          attrs: isSelected ? [["value", option.target], ["selected", ""]] : [["value", option.target]],
        })
      );
    }
  }

  const typeSelect = h("select", {
    class: "field__control",
    attrs: [["id", "instance-type"], ["name", "target_type"], ["required", ""]],
    onChange: (event) => {
      formState.target_type = event.target.value;
    },
    children: typeOptionsNodes,
  });

  const grid = h("div", {
    class: "form-grid",
    children: [
      h("div", {
        class: "field",
        children: [
          h("label", {
            class: "field__label",
            text: "Name",
            attrs: [["for", "instance-name"]],
          }),
          nameInput,
          h("p", {
            class: "field__help",
            text: "Required. At most " + String(MAX_NAME_LENGTH) + " characters.",
          }),
        ],
      }),
      h("div", {
        class: "field",
        children: [
          h("label", {
            class: "field__label",
            text: "Target type",
            attrs: [["for", "instance-type"]],
          }),
          typeSelect,
          h("p", {
            class: "field__help",
            text:
              options.length === 0
                ? "No target types are available yet."
                : "The types the platform can plan against, taken from the target catalog.",
          }),
        ],
      }),
      h("div", {
        class: "field",
        children: [
          h("label", {
            class: "field__label",
            text: "Description (optional)",
            attrs: [["for", "instance-description"]],
          }),
          descInput,
          h("p", {
            class: "field__help",
            text: "Optional. At most " + String(MAX_DESCRIPTION_LENGTH) + " characters.",
          }),
        ],
      }),
      h("div", {
        class: "actions",
        children: [submit, NavLink("Back to Dashboard", "/dashboard")],
      }),
    ],
  });

  render(formSlot, [grid]);
}

/** Guards the create form against double-submission while a request is pending. */
let submittingInstance = false;

async function submitForm() {
  const problems = validateForm(formState);
  if (problems.length > 0) {
    renderFeedback(
      "error",
      "Please fix the following before submitting - " + problems.join(" ")
    );
    return;
  }
  if (submittingInstance) {
    return;
  }
  submittingInstance = true;
  try {
    // Copy so a failure leaves the form untouched for a retry.
    const payload = {
      name: formState.name,
      target_type: formState.target_type,
      description: formState.description || "",
    };
    renderFeedback(null, "");
    render(formSlot, [pending("Creating target instance...")]);
    const res = await api.post("/api/target-instances", payload);
    if (!res.ok) {
      // Surface the API's own message (already normalised by js/api.js) and keep
      // the entered values so the user can correct and resubmit.
      renderFeedback("error", res.error.message);
      renderForm();
      return;
    }
    const name = res.data && typeof res.data.name === "string" ? res.data.name : payload.name;
    renderFeedback("success", `Created target instance "${name}".`);
    // Clear only after a successful create, then refresh the list.
    formState = { name: "", target_type: "", description: "" };
    renderForm();
    renderList();
  } finally {
    submittingInstance = false;
  }
}

/* ---- delete --------------------------------------------------------------- */

/**
 * Confirm, then delete, then refresh. The confirmation is a required second
 * click on a distinct state, so a single misclick cannot remove an instance.
 * GET /api/target-instances/{id} is intentionally not used: the confirmation
 * names the target instance from the row the user clicked.
 */
function confirmAndDelete(instance) {
  const slot = listSlot;
  if (!slot) {
    return;
  }
  const label = isNonEmptyString(instance.name) ? instance.name : instance.id;
  render(slot, [
    h("div", {
      class: "state state--error",
      children: [
        h("p", {
          class: "state__title",
          text: `Delete target instance "${label}"?`,
        }),
        h("p", {
          class: "panel__hint",
          text: "This removes the instance. Jobs already recorded against it are not affected.",
        }),
        h("div", {
          class: "actions",
          children: [
            button(
              "Yes, delete",
              () => {
                processDelete(instance.id, label);
              },
              "btn btn--danger"
            ),
            button("Cancel", renderList),
          ],
        }),
      ],
    }),
  ]);
}

/** Guards delete against a second click while a request is pending. */
let deletingInstanceId = "";

async function processDelete(instanceId, label) {
  const slot = listSlot;
  if (!slot) {
    return;
  }
  if (deletingInstanceId) {
    return;
  }
  deletingInstanceId = instanceId;
  render(slot, [pending(`Deleting target instance "${label}"...`)]);
  try {
    // The shared client treats 204 (no body) as a success with data === null.
    const res = await api.delete(`/api/target-instances/${instanceId}`);
    if (!res.ok) {
      render(slot, [
        InlineError(res.error),
        RetryButton(renderList),
        h("p", {
          class: "panel__hint",
          text: "The target instance was not deleted.",
        }),
      ]);
      return;
    }
    renderFeedback("success", `Deleted target instance "${label}".`);
    // Refresh only after a successful delete.
    renderList();
  } finally {
    deletingInstanceId = "";
  }
}

/* ---- view ----------------------------------------------------------------- */

async function renderInstances(root) {
  listSlot = h("div", { class: "section-body" });
  formSlot = h("div", { class: "section-body" });
  feedbackSlot = h("div", { class: "section-body" });
  filterSlot = h("div", { class: "section-body" });
  // Reset the form on every entry so values never leak across navigations.
  formState = { name: "", target_type: "", description: "" };
  paginationState = {
    limit: 50,
    offset: 0,
    total: 0,
    targetTypeFilter: "",
    nameFilter: "",
  };

  render(root, [
    SimulationNotice(),
    Panel(
      "Target instances",
      [
        h("div", { class: "section-body" }),
        h("p", {
          class: "panel__hint",
          text:
            "A target instance is a named thing to audit. Simulated jobs can be associated with one, and the association is a snapshot taken when the job is created.",
        }),
        filterSlot,
        listSlot,
      ],
      "Create, inspect and remove target instances."
    ),
    Panel("Create a target instance", [h("div", { class: "section-body" }), feedbackSlot, formSlot]),
    Panel(
      "Simulation only",
      [
        h("p", {
          class: "panel__hint",
          text:
            "Creating or deleting a target instance does not run anything. No engine binary is invoked, no attack is performed, and no capability data is probed from the browser.",
        }),
      ]
    ),
  ]);

  // Load the type options first so the form can render a populated picker, then
  // render the form and the list. Both failures are independent.
  await loadTargetTypes();
  renderFilters();
  renderForm();
  renderList();
}

/* ---- registration --------------------------------------------------------- */

export const InstancesView = registerView("/instances", {
  title: "Instances",
  render: (root) => {
    renderInstances(root).catch((err) => {
      render(root, [SimulationNotice(), InlineError(err)]);
    });
  },
});

export default InstancesView;
