/**
 * Translate a thrown value into a message the interface can show.
 *
 * `ApiError.detail` is echoed verbatim: the server's problem detail is
 * already written for people, and paraphrasing security-sensitive messages
 * (login failures in particular) risks reintroducing what the server removed.
 */
import { ApiError, NetworkError } from "@/api/client";
import { i18n } from "@/app/i18n";

export interface Failure {
  message: string;
  code: string;
  status?: number;
  requestId?: string;
}

export function describeError(caught: unknown): Failure {
  const t = i18n.global.t;
  if (caught instanceof ApiError) {
    const failure: Failure = { message: caught.detail, code: caught.code, status: caught.status };
    if (caught.requestId) failure.requestId = caught.requestId;
    return failure;
  }
  if (caught instanceof NetworkError) {
    return { message: t("errors.network"), code: "NETWORK" };
  }
  if (caught instanceof DOMException && caught.name === "AbortError") {
    return { message: "", code: "ABORTED" };
  }
  if (caught instanceof Error && caught.message) {
    return { message: caught.message, code: "UNKNOWN" };
  }
  return { message: t("errors.unexpected"), code: "UNKNOWN" };
}

export function isAbort(caught: unknown): boolean {
  return caught instanceof DOMException && caught.name === "AbortError";
}
