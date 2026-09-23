<script setup lang="ts">
/**
 * Inline notice: the quieter cousin of the toast for messages that belong to
 * a specific place on the page (a form, a section) and should persist there.
 */
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-vue-next";

import { cn } from "@/lib/utils";

withDefaults(
  defineProps<{ tone?: "info" | "ok" | "warn" | "bad"; title?: string; dismissable?: boolean; class?: string; testId?: string }>(),
  { tone: "info" },
);
const emit = defineEmits<{ dismiss: [] }>();

const icons = { info: Info, ok: CheckCircle2, warn: AlertTriangle, bad: XCircle };
const styles = {
  info: "border-info/25 bg-info-soft text-ink [&_svg]:text-info",
  ok: "border-ok/25 bg-ok-soft text-ink [&_svg]:text-ok",
  warn: "border-warn/30 bg-warn-soft text-ink [&_svg]:text-warn",
  bad: "border-bad/25 bg-bad-soft text-ink [&_svg]:text-bad",
};
</script>

<template>
  <div :role="tone === 'bad' ? 'alert' : 'status'" :class="cn('flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-[13px] leading-snug', styles[tone], $props.class)" :data-test="testId">
    <component :is="icons[tone]" class="mt-px size-4 shrink-0" aria-hidden="true" />
    <div class="min-w-0 flex-1">
      <p v-if="title" class="font-medium">{{ title }}</p>
      <div :class="title ? 'mt-0.5 text-ink-2' : ''"><slot /></div>
    </div>
    <button
      v-if="dismissable"
      type="button"
      class="-mr-1 inline-flex size-6 shrink-0 items-center justify-center rounded-xs text-ink-3 hover:bg-black/5 hover:text-ink dark:hover:bg-white/10"
      :aria-label="$t('common.close')"
      @click="emit('dismiss')"
    >
      <X class="size-3.5" aria-hidden="true" />
    </button>
  </div>
</template>
