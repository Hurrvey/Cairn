<script setup lang="ts">
import { computed, useAttrs } from "vue";

import { cn } from "@/lib/utils";

defineOptions({ inheritAttrs: false });

const props = withDefaults(
  defineProps<{
    invalid?: boolean;
    size?: "sm" | "md";
    class?: string;
    inputClass?: string;
  }>(),
  { size: "md" },
);

const model = defineModel<string | number | null>({ default: "" });
const attrs = useAttrs();

const wrapperClass = computed(() =>
  cn(
    "group flex items-center gap-2 rounded-sm border bg-surface text-ink shadow-[inset_0_1px_1px_rgb(26_27_25/0.03)] transition-[border-color,box-shadow] duration-150 focus-within:border-brand focus-within:ring-2 focus-within:ring-brand/25",
    props.size === "sm" ? "h-7 px-2 text-[12.5px]" : "h-8 px-2.5 text-[13px]",
    props.invalid ? "border-bad focus-within:border-bad focus-within:ring-bad/25" : "border-line-strong",
    attrs.disabled ? "opacity-60 bg-surface-2" : "",
    props.class,
  ),
);

function onInput(event: Event): void {
  const target = event.target as HTMLInputElement;
  model.value = target.type === "number" ? (target.value === "" ? null : Number(target.value)) : target.value;
}
</script>

<template>
  <label :class="wrapperClass">
    <span v-if="$slots.prefix" class="flex shrink-0 items-center text-ink-3 [&_svg]:size-4"><slot name="prefix" /></span>
    <input
      v-bind="attrs"
      :value="model ?? ''"
      :aria-invalid="invalid ? 'true' : undefined"
      :class="cn('min-w-0 flex-1 bg-transparent outline-none placeholder:text-ink-3 disabled:cursor-not-allowed', inputClass)"
      @input="onInput"
    />
    <span v-if="$slots.suffix" class="flex shrink-0 items-center text-ink-3 [&_svg]:size-4"><slot name="suffix" /></span>
  </label>
</template>
