<script setup lang="ts">
import { onMounted, onUnmounted } from "vue";

import { onApiError } from "@/api/client";
import { useSessionStore } from "@/stores/session";

const session = useSessionStore();
let unsubscribe: (() => void) | null = null;

onMounted(() => {
  // The global hook that makes FR-P-02 hold everywhere: a 403
  // PASSWORD_CHANGE_REQUIRED from ANY endpoint reopens the dialog, so there is
  // no path — including a stale tab left open across a restart — that renders
  // the application while the requirement is outstanding (TC-M16-03).
  unsubscribe = onApiError("PASSWORD_CHANGE_REQUIRED", () => {
    session.requireCredentialChange();
  });
});

onUnmounted(() => unsubscribe?.());
</script>

<template>
  <RouterView />
</template>
