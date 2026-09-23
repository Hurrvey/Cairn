<script setup lang="ts" generic="T extends string">
/**
 * Segmented control used as a tab list. Arrow keys move between segments so
 * a keyboard user can switch views without tabbing through every button.
 */
import { cn } from "@/lib/utils";

export interface Segment<V extends string> {
  value: V;
  label: string;
  count?: number | null;
  testId?: string;
}

const props = defineProps<{ options: Segment<T>[]; ariaLabel?: string; class?: string; size?: "sm" | "md" }>();
const model = defineModel<T>({ required: true });

function move(delta: number, current: T): void {
  const index = props.options.findIndex((option) => option.value === current);
  const next = props.options[(index + delta + props.options.length) % props.options.length];
  if (next) {
    model.value = next.value;
    requestAnimationFrame(() => {
      const button = document.querySelector<HTMLButtonElement>(`[data-segment="${next.value}"]`);
      button?.focus();
    });
  }
}
</script>

<template>
  <div
    role="tablist"
    :aria-label="ariaLabel"
    :class="cn('inline-flex max-w-full items-center gap-0.5 overflow-x-auto rounded-md border border-line bg-surface-2 p-0.5', $props.class)"
  >
    <button
      v-for="option in options"
      :key="option.value"
      type="button"
      role="tab"
      :aria-selected="model === option.value"
      :tabindex="model === option.value ? 0 : -1"
      :data-segment="option.value"
      :data-test="option.testId"
      :class="
        cn(
          'inline-flex shrink-0 items-center gap-1.5 rounded-sm px-2.5 font-medium text-ink-2 transition-colors outline-none focus-visible:ring-2 focus-visible:ring-brand/40',
          size === 'sm' ? 'h-6 text-[12px]' : 'h-7 text-[13px]',
          model === option.value ? 'bg-surface text-ink shadow-[0_1px_2px_rgb(26_27_25/0.08)]' : 'hover:text-ink',
        )
      "
      @click="model = option.value"
      @keydown.left.prevent="move(-1, option.value)"
      @keydown.right.prevent="move(1, option.value)"
    >
      {{ option.label }}
      <span
        v-if="option.count !== undefined && option.count !== null"
        class="tnum rounded-full bg-surface-3 px-1.5 text-[11px] text-ink-3"
      >
        {{ option.count }}
      </span>
    </button>
  </div>
</template>
