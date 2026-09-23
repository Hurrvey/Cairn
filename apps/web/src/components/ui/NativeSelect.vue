<script setup lang="ts">
import { ChevronDown } from "lucide-vue-next";
import { computed, useAttrs } from "vue";

import { cn } from "@/lib/utils";

defineOptions({ inheritAttrs: false });

const props = withDefaults(
  defineProps<{ invalid?: boolean; size?: "sm" | "md"; class?: string }>(),
  { size: "md" },
);
const model = defineModel<string | number | null>({ default: "" });
const attrs = useAttrs();

const classes = computed(() =>
  cn(
    "w-full appearance-none rounded-sm border bg-surface pr-8 text-ink shadow-[inset_0_1px_1px_rgb(26_27_25/0.03)] outline-none transition-[border-color,box-shadow] focus:border-brand focus:ring-2 focus:ring-brand/25 disabled:cursor-not-allowed disabled:opacity-60",
    props.size === "sm" ? "h-7 pl-2 text-[12.5px]" : "h-8 pl-2.5 text-[13px]",
    props.invalid ? "border-bad" : "border-line-strong",
    props.class,
  ),
);

function onChange(event: Event): void {
  const target = event.target as HTMLSelectElement;
  const raw = target.value;
  model.value = typeof model.value === "number" ? Number(raw) : raw;
}
</script>

<template>
  <span class="relative inline-flex w-full items-center">
    <select v-bind="attrs" :value="model ?? ''" :class="classes" @change="onChange">
      <slot />
    </select>
    <ChevronDown class="pointer-events-none absolute right-2 size-4 text-ink-3" aria-hidden="true" />
  </span>
</template>
