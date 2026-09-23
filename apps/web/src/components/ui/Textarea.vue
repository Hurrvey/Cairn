<script setup lang="ts">
import { computed, useAttrs } from "vue";

import { cn } from "@/lib/utils";

defineOptions({ inheritAttrs: false });

const props = defineProps<{ invalid?: boolean; class?: string; mono?: boolean }>();
const model = defineModel<string>({ default: "" });
const attrs = useAttrs();

const classes = computed(() =>
  cn(
    "block w-full min-h-20 rounded-sm border bg-surface px-2.5 py-2 text-[13px] leading-relaxed text-ink shadow-[inset_0_1px_1px_rgb(26_27_25/0.03)] outline-none transition-[border-color,box-shadow] placeholder:text-ink-3 focus:border-brand focus:ring-2 focus:ring-brand/25 disabled:cursor-not-allowed disabled:opacity-60",
    props.invalid ? "border-bad focus:border-bad focus:ring-bad/25" : "border-line-strong",
    props.mono ? "font-mono text-[12.5px]" : "",
    props.class,
  ),
);
</script>

<template>
  <textarea
    v-bind="attrs"
    :value="model"
    :aria-invalid="invalid ? 'true' : undefined"
    :class="classes"
    @input="model = ($event.target as HTMLTextAreaElement).value"
  />
</template>
