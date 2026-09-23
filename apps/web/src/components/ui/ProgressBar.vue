<script setup lang="ts">
import { computed } from "vue";

import { cn } from "@/lib/utils";

const props = withDefaults(
  defineProps<{ value: number; tone?: "brand" | "ok" | "warn" | "bad"; size?: "sm" | "md"; label?: string; class?: string; indeterminate?: boolean }>(),
  { tone: "brand", size: "sm" },
);
const clamped = computed(() => Math.max(0, Math.min(100, props.value)));
const fill = { brand: "bg-brand", ok: "bg-ok", warn: "bg-warn", bad: "bg-bad" };
</script>

<template>
  <div
    role="progressbar"
    :aria-valuenow="indeterminate ? undefined : clamped"
    aria-valuemin="0"
    aria-valuemax="100"
    :aria-label="label"
    :class="cn('relative w-full overflow-hidden rounded-full bg-surface-3', size === 'sm' ? 'h-1.5' : 'h-2.5', $props.class)"
  >
    <div
      :class="cn('h-full rounded-full transition-[width] duration-500 ease-out', fill[tone], indeterminate && 'w-1/3 animate-[shimmer_1.4s_ease-in-out_infinite]')"
      :style="indeterminate ? undefined : { width: `${clamped}%` }"
    />
  </div>
</template>
