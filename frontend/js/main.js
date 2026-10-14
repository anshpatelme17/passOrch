/*
 * Frontend entry point (Phase 4A/4B).
 *
 * Imports the view modules - which register themselves with the router - and
 * then starts the shell. Keeping the view list here rather than in js/app.js
 * means the router stays generic and each phase adds a view by adding one
 * import, without touching the shell or the other phases' views.
 *
 * SECURITY BOUNDARY
 * No rendering or network code of its own; it only wires existing modules.
 */

import "./views/dashboard.js";
import "./views/targets.js";
import "./views/instances.js";
import "./views/plan.js";
import "./views/jobs.js";
import "./views/engines.js";
import { start } from "./app.js";

start();
