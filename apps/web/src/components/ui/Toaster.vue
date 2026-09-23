<script setup lang="ts">
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-vue-next";

import { useToasts } from "@/shared/composables/useToasts";

const toasts = useToasts();
const icons = { success: CheckCircle2, error: XCircle, info: Info, warning: AlertTriangle };
const tones = { success: "text-ok", error: "text-bad", info: "text-info", warning: "text-warn" };
</script>

<template>
  <div class="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(380px,calc(100vw-2rem))] flex-col gap-2" aria-live="polite" aria-atomic="false">
    <TransitionGroup name="toast">
      <div
        v-for="toast in toasts.items"
        :key="toast.id"
        role="status"
        class="pointer-events-auto flex items-start gap-2.5 rounded-md border border-line bg-surface px-3 py-2.5 shadow-pop"
        :data-test="`toast-${toast.kind}`"
      >
        <component :is="icons[toast.kind]" :class="['mt-0.5 size-4 shrink-0', tones[toast.kind]]" aria-hidden="true" />
        <div class="min-w-0 flex-1">
          <p class="text-[13px] font-medium leading-snug text-ink">{{ toast.title }}</p>
          <p v-if="toast.detail" class="mt-0.5 break-words text-[12px] leading-snug text-ink-2">{{ toast.detail }}</p>
        </div>
        <button
          type="button"
          class="-mr-1 -mt-0.5 inline-flex size-6 shrink-0 items-center justify-center rounded-xs text-ink-3 hover:bg-surface-3 hover:text-ink"
          :aria-label="$t('common.close')"
          @click="toasts.dismiss(toast.id)"
        >
          <X class="size-3.5" aria-hidden="true" />
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-enter-active,
.toast-leave-active {
  transition:
    opacity 160ms ease,
    transform 200ms var(--ease-out-soft);
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
@media (prefers-reduced-motion: reduce) {
  .toast-enter-active,
  .toast-leave-active {
    transition: none;
  }
}
</style>
