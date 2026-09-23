<script setup lang="ts">
import { cn } from "@/lib/utils";

withDefaults(defineProps<{ title: string; description?: string; compact?: boolean; class?: string }>(), {
  compact: false,
});
</script>

<template>
  <div
    :class="cn('grid justify-items-center gap-2 text-center', compact ? 'px-4 py-8' : 'px-6 py-14', $props.class)"
    data-test="empty-state"
  >
    <span v-if="$slots.icon" class="mb-1 flex size-10 items-center justify-center rounded-lg bg-surface-3 text-ink-3 [&_svg]:size-5">
      <slot name="icon" />
    </span>
    <h3 class="font-display text-[15px] font-semibold text-ink">{{ title }}</h3>
    <p v-if="description" class="max-w-md text-[13px] leading-relaxed text-ink-2">{{ description }}</p>
    <div v-if="$slots.default" class="mt-2 flex flex-wrap justify-center gap-2"><slot /></div>
  </div>
</template>
