/**
 * HTTP transport.
 *
 * Three responsibilities beyond fetch:
 *
 * 1. Parse RFC 9457 `problem+json` into a typed `ApiError` carrying the stable
 *    machine `code`. UI branches on `code`, never on `detail` — `detail` is
 *    human-facing prose that may be reworded or localised at any time.
 * 2. Attach the CSRF token on state-changing requests (NFR-SEC-04).
 * 3. Surface `PASSWORD_CHANGE_REQUIRED` from *any* endpoint to a single
 *    listener. This is what makes FR-P-02 hold globally: the dialog reopens no
 *    matter which call tripped the guard, so there is no route that can quietly
 *    render while the requirement is outstanding.
 *
 * It also reports transport health so the shell can show an offline notice
 * after a failed connection and clear it on the next successful response.
 */

const CSRF_COOKIE = "cairn_csrf";
const CSRF_HEADER = "X-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export interface FieldError {
  field: string;
  code: string;
  detail: string;
  value?: unknown;
}

/** A parsed problem+json response. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;
  readonly requestId: string | undefined;
  readonly errors: FieldError[];

  constructor(status: number, body: Record<string, unknown>) {
    const detail =
      typeof body.detail === "string" ? body.detail : "Request failed.";
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.code = typeof body.code === "string" ? body.code : "UNKNOWN";
    this.detail = detail;
    this.requestId =
      typeof body.request_id === "string" ? body.request_id : undefined;
    this.errors = Array.isArray(body.errors)
      ? (body.errors as FieldError[])
      : [];
  }

  /** Field-level messages keyed by field name, for inline form errors. */
  byField(): Record<string, string[]> {
    const grouped: Record<string, string[]> = {};
    for (const error of this.errors) {
      (grouped[error.field] ??= []).push(error.detail);
    }
    return grouped;
  }
}

export class NetworkError extends Error {
  constructor(cause: unknown) {
    super("The server could not be reached.");
    this.name = "NetworkError";
    this.cause = cause;
  }
}

type Listener = (error: ApiError) => void;

const listeners: Record<string, Listener[]> = {};

/** Subscribe to a specific error code from anywhere in the app. */
export function onApiError(code: string, listener: Listener): () => void {
  (listeners[code] ??= []).push(listener);
  return () => {
    listeners[code] = (listeners[code] ?? []).filter((l) => l !== listener);
  };
}

function emit(error: ApiError): void {
  for (const listener of listeners[error.code] ?? []) listener(error);
}

type TransportListener = (reachable: boolean) => void;
const transportListeners: TransportListener[] = [];

/** Subscribe to transport health: false after a connection failure, true
 *  after the next response of any status. */
export function onTransport(listener: TransportListener): () => void {
  transportListeners.push(listener);
  return () => {
    const index = transportListeners.indexOf(listener);
    if (index >= 0) transportListeners.splice(index, 1);
  };
}

function reportTransport(reachable: boolean): void {
  for (const listener of transportListeners) listener(reachable);
}

function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match?.[1] ? decodeURIComponent(match[1]) : null;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  /** Bearer token. Used for the scope-limited change token, which is deliberately
   *  never stored in a cookie — it must not be an ambient credential. */
  token?: string | undefined;
  signal?: AbortSignal;
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { Accept: "application/json" };

  const multipart = options.body instanceof FormData;
  if (options.body !== undefined && !multipart)
    headers["Content-Type"] = "application/json";
  if (options.token) headers["Authorization"] = `Bearer ${options.token}`;

  if (!SAFE_METHODS.has(method) && !options.token) {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers[CSRF_HEADER] = csrf;
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: multipart
        ? (options.body as FormData)
        : options.body === undefined
          ? undefined
          : JSON.stringify(options.body),
      ...(options.signal ? { signal: options.signal } : {}),
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError")
      throw cause;
    reportTransport(false);
    throw new NetworkError(cause);
  }
  reportTransport(true);

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = {};
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      // A proxy error page is not JSON; keep the status and a generic detail.
      payload = { code: response.ok ? "UNKNOWN" : "UPSTREAM_ERROR", detail: text.slice(0, 200) };
    }
  }

  if (!response.ok) {
    const error = new ApiError(
      response.status,
      payload as Record<string, unknown>,
    );
    emit(error);
    throw error;
  }

  return payload as T;
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "GET" }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "POST", body }),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "PATCH", body }),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "PUT", body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    request<T>(path, { ...options, method: "DELETE" }),
};
