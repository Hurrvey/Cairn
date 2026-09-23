<script setup lang="ts">
import { cva } from "class-variance-authority";
import { computed } from "vue";

import type { Tone } from "@/components/ui/types";
import { cn } from "@/lib/utils";

const props = withDefaults(
  defineProps<{
    tone?: Tone;
    dot?: boolean;
    pulse?: boolean;
    size?: "sm" | "md";
    mono?: boolean;
    class?: string;
  }>(),
  { tone: "neutral", size: "md" },
);

const styles = cva(
  "inline-flex max-w-full items-center gap-1.5 rounded-full border font-medium leading-none whitespace-nowrap",
  {
    variants: {
      tone: {
        neutral: "border-line bg-surface-2 text-ink-2",
        brand: "border-brand/25 bg-brand-soft text-brand-strong",
        ok: "border-ok/25 bg-ok-soft text-ok",
        warn: "border-warn/30 bg-warn-soft text-warn",
        bad: "border-bad/25 bg-bad-soft text-bad",
        info: "border-info/25 bg-info-soft text-info",
      },
      size: {
        sm: "h-5 px-1.5 text-[11px]",
        md: "h-6 px-2 text-[12px]",
      },
    },
  },
);

const classes = computed(() => cn(styles({ tone: props.tone, size: props.size }), props.mono && "font-mono", props.class));
</script>

<template>
  <span :class="classes">
    <span
      v-if="dot"
      :class="cn('size-1.5 shrink-0 rounded-full bg-current', pulse && 'pulse-dot')"
      aria-hidden="true"
    />
    <span class="truncate"><slot /></span>
  </span>
</template>
