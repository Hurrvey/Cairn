/**
 * Toast notifications. A small module-level store so any component or API
 * helper can announce an outcome without prop drilling.
 */
import { reactive } from "vue";

export type ToastKind = "success" | "error" | "info" | "warning";

export interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  detail?: string;
  /** Milliseconds before auto-dismiss; 0 keeps it until closed. */
  duration: number;
}

const state = reactive<{ items: Toast[] }>({ items: [] });
let sequence = 0;

export function pushToast(
  kind: ToastKind,
  title: string,
  options: { detail?: string; duration?: number } = {},
): number {
  const id = ++sequence;
  const duration = options.duration ?? (kind === "error" ? 8000 : 4000);
  const toast: Toast = { id, kind, title, duration };
  if (options.detail) toast.detail = options.detail;
  state.items.push(toast);
  if (state.items.length > 5) state.items.shift();
  if (duration > 0 && typeof window !== "undefined") {
    window.setTimeout(() => dismissToast(id), duration);
  }
  return id;
}

export function dismissToast(id: number): void {
  const index = state.items.findIndex((item) => item.id === id);
  if (index >= 0) state.items.splice(index, 1);
}

export function clearToasts(): void {
  state.items.splice(0, state.items.length);
}

export function useToasts() {
  return {
    items: state.items,
    success: (title: string, detail?: string) => pushToast("success", title, { detail }),
    error: (title: string, detail?: string) => pushToast("error", title, { detail }),
    info: (title: string, detail?: string) => pushToast("info", title, { detail }),
    warning: (title: string, detail?: string) => pushToast("warning", title, { detail }),
    dismiss: dismissToast,
  };
}
