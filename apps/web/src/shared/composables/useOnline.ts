/**
 * Connectivity: combines the browser's own online flag with transport
 * failures reported by the API client, so a dead proxy shows up even when
 * the OS still thinks the network is fine.
 */
import { onBeforeUnmount, onMounted, ref } from "vue";

import { onTransport } from "@/api/client";

export function useOnline() {
  const browserOnline = ref(typeof navigator === "undefined" ? true : navigator.onLine);
  const serverReachable = ref(true);

  const onOnline = () => (browserOnline.value = true);
  const onOffline = () => (browserOnline.value = false);
  let unsubscribe: (() => void) | null = null;

  onMounted(() => {
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    unsubscribe = onTransport((reachable) => (serverReachable.value = reachable));
  });
  onBeforeUnmount(() => {
    window.removeEventListener("online", onOnline);
    window.removeEventListener("offline", onOffline);
    unsubscribe?.();
  });

  return { browserOnline, serverReachable };
}
