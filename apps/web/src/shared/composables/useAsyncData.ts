/**
 * Minimal async-data helper: load, error, refresh.
 *
 * Deliberately not TanStack Query. Phase 1 has five screens with no shared
 * cache keys and no background refetching; a dependency whose value only
 * appears at scale is a cost paid now for a benefit later. Revisit when the
 * document list needs polling (T-M16-12).
 */

import { ref, shallowRef, type Ref } from "vue";

import { ApiError, NetworkError } from "@/api/client";

export interface AsyncData<T> {
  data: Ref<T | null>;
  loading: Ref<boolean>;
  error: Ref<string | null>;
  errorCode: Ref<string | null>;
  refresh: () => Promise<void>;
}

export function useAsyncData<T>(
  loader: () => Promise<T>,
  options: { immediate?: boolean } = {},
): AsyncData<T> {
  const data = shallowRef<T | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const errorCode = ref<string | null>(null);

  async function refresh(): Promise<void> {
    loading.value = true;
    error.value = null;
    errorCode.value = null;
    try {
      data.value = await loader();
    } catch (caught) {
      if (caught instanceof ApiError) {
        error.value = caught.detail;
        errorCode.value = caught.code;
      } else if (caught instanceof NetworkError) {
        error.value = "The server could not be reached.";
        errorCode.value = "NETWORK";
      } else {
        error.value = "Something went wrong.";
        errorCode.value = "UNKNOWN";
      }
    } finally {
      loading.value = false;
    }
  }

  if (options.immediate !== false) void refresh();

  return { data, loading, error, errorCode, refresh };
}
