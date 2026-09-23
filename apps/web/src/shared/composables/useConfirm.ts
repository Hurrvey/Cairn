/**
 * Promise-based confirmation. `confirm({...})` resolves true when the user
 * accepts, false when they cancel or dismiss. Rendered once by ConfirmHost in
 * the app shell so every page shares the same dialog.
 */
import { reactive } from "vue";

export interface ConfirmOptions {
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Destructive confirmations render the accept button in the danger style. */
  danger?: boolean;
}

interface Pending extends ConfirmOptions {
  resolve: (value: boolean) => void;
}

const state = reactive<{ current: Pending | null }>({ current: null });

export function confirm(options: ConfirmOptions): Promise<boolean> {
  // A second request while one is open cancels the first: the user can only
  // answer one question at a time, and stacking would hide it.
  state.current?.resolve(false);
  return new Promise<boolean>((resolve) => {
    state.current = { ...options, resolve };
  });
}

export function useConfirmState() {
  function settle(value: boolean): void {
    const current = state.current;
    state.current = null;
    current?.resolve(value);
  }
  return { state, settle };
}
