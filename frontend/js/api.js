/*
 * PassOrch shared API client.
 *
 * Responsibilities - deliberately the only place that knows about `fetch`:
 *   - issue GET / POST / DELETE with JSON bodies;
 *   - separate HTTP errors from transport errors;
 *   - tolerate 204 (no body) and empty or malformed JSON;
 *   - normalise the two distinct error payload shapes the backend produces
 *     (structured object detail, or a Pydantic validation-error array);
 *   - never throw on an HTTP error status (callers inspect `res.error`).
 *
 * SECURITY BOUNDARY
 * No innerHTML anywhere; only fetch of first-party /api and /health paths; the
 * caller-supplied path is validated against a closed prefix set, so a bad
 * argument can never redirect this client to an arbitrary origin.
 */

export const API_PREFIXES = Object.freeze(["/api/", "/health"]);

/**
 * Stable machine codes the backend returns in the structured object detail
 * ({"detail": {"error": code, "detail": message, ...}}).
 */
export const API_ERRORS = Object.freeze({
  unknownTarget: "unknown_target",
  instanceNotFound: "target_instance_not_found",
  instanceTypeMismatch: "target_instance_type_mismatch",
  invalidRequest: "invalid_request",
  jobNotFound: "job_not_found",
  targetNotMapped: "target_not_mapped",
  unknownEngine: "unknown_engine",
  engineNotRegistered: "engine_not_registered",
  unauthorized: "unauthorized",
  rateLimited: "rate_limited",
  payloadTooLarge: "payload_too_large",
  unsupportedTransferEncoding: "unsupported_transfer_encoding",
  persistenceUnavailable: "persistence_unavailable",
  notReady: "not_ready",
});

/**
 * Reduce a FastAPI error detail to a short human string.
 *
 * The backend emits two shapes:
 *   - an array of "loc/msg" objects (Pydantic `HTTPValidationError`); or
 *   - a structured object with `error` and free-form fields (domain errors).
 *
 * Anything else falls back to the status line so nothing is silently dropped.
 */
export function describeError(status, detail) {
  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry) => {
        if (entry === null || typeof entry !== "object") {
          return typeof entry === "string" ? entry : null;
        }
        const where = Array.isArray(entry.loc) ? entry.loc.join(".") : "";
        const msg = typeof entry.msg === "string" ? entry.msg : "";
        return [where, msg].filter(Boolean).join(": ");
      })
      .filter(Boolean);
    if (parts.length) {
      return "Validation failed - " + parts.map((p) => `[${p}]`).join(" ");
    }
    return `Request rejected (${status})`;
  }

  if (detail !== null && typeof detail === "object") {
    const code = typeof detail.error === "string" ? detail.error : "";
    const text = typeof detail.detail === "string" ? detail.detail : "";
    if (code && text) {
      return text.replace(/\.$/, "") + ` (${code})`;
    }
    if (code) {
      return code;
    }
    if (text) {
      return text;
    }
    return `Request rejected (${status})`;
  }

  if (typeof detail === "string" && detail) {
    return detail;
  }
  return `Request failed (${status})`;
}

function assertPath(path) {
  if (typeof path !== "string" || !path.startsWith("/")) {
    throw new Error("api path must start with '/'");
  }
  const allowed = API_PREFIXES.some((prefix) => path.startsWith(prefix));
  if (!allowed) {
    throw new Error(`api path must start with one of: ${API_PREFIXES.join(", ")}`);
  }
  return path;
}

async function parseBody(response) {
  // 204 and any other body-less response: never call .json() on empty text.
  if (response.status === 204) {
    return { data: null, raw: null, parseError: false };
  }
  const text = await response.text();
  if (!text) {
    return { data: null, raw: null, parseError: false };
  }
  try {
    return { data: JSON.parse(text), raw: text, parseError: false };
  } catch {
    // A server error or a proxy may answer with a non-JSON body. Surface it as
    // text rather than throwing away the status code.
    return { data: null, raw: text, parseError: true };
  }
}

/**
 * Perform one request.
 *
 * Resolves for every outcome; `res.ok` is the HTTP-2xx indicator and `res.error`
 * is populated for any non-2xx response or transport failure.
 *
 * @param {string} path
 * @param {{method?: "GET"|"POST"|"PATCH"|"DELETE", body?: object, signal?: AbortSignal}} [options]
 * @returns {Promise<{ok: boolean, status: number, data: any, raw: string|null,
 *                    parseError: boolean, error: {kind: string, message: string,
 *                    code: string, detail: any}|null}>}
 */
export async function request(path, options) {
  const opts = options || {};
  let url;
  try {
    url = assertPath(path);
  } catch (err) {
    return {
      ok: false,
      status: 0,
      data: null,
      raw: null,
      parseError: false,
      error: {
        kind: "invalid_request",
        message: String((err && err.message) || err),
        code: "",
        detail: null,
      },
    };
  }

  let response;
  try {
    const headers = { Accept: "application/json" };
    if (opts.body !== undefined && opts.method !== "GET") {
      headers["Content-Type"] = "application/json";
    }
    response = await fetch(url, {
      method: opts.method || "GET",
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: opts.signal,
    });
  } catch (err) {
    // Network-level failure: no status, no body.
    return {
      ok: false,
      status: 0,
      data: null,
      raw: null,
      parseError: false,
      error: {
        kind: "transport",
        message: String((err && err.message) || "network request failed"),
        code: "",
        detail: null,
      },
    };
  }

  const parsed = await parseBody(response);
  if (response.ok) {
    return {
      ok: true,
      status: response.status,
      data: parsed.data,
      raw: parsed.raw,
      parseError: parsed.parseError,
      error: null,
    };
  }

  // FastAPI wraps every error payload in a "detail" envelope:
  //   {"detail": {...}}   for a structured domain error, or
  //   {"detail": [...]}   for a Pydantic validation error.
  // Unwrap once here so `code` and `detail` refer to the inner payload.
  const envelope =
    parsed.data !== null && typeof parsed.data === "object" && !Array.isArray(parsed.data)
      ? parsed.data
      : null;
  const detail = parsed.parseError ? null : envelope ? envelope.detail : parsed.data;
  const code =
    detail !== null && typeof detail === "object" && !Array.isArray(detail) && typeof detail.error === "string"
      ? detail.error
      : "";

  return {
    ok: false,
    status: response.status,
    data: parsed.data,
    raw: parsed.raw,
    parseError: parsed.parseError,
    error: {
      kind: "http",
      // On a parse error `detail` is null, so describeError falls back to a
      // status line. The raw response text is never shown: it may be a proxy
      // HTML page and is not a useful (or safe) user-facing message.
      message: describeError(response.status, detail),
      code,
      detail,
    },
  };
}

export const api = Object.freeze({
  get(path, signal) {
    return request(path, { method: "GET", signal });
  },
  post(path, body, signal) {
    return request(path, { method: "POST", body, signal });
  },
  patch(path, body, signal) {
    return request(path, { method: "PATCH", body, signal });
  },
  delete(path, signal) {
    return request(path, { method: "DELETE", signal });
  },
});

export default api;
