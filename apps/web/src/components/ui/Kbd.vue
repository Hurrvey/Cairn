<script setup lang="ts">
import { cn } from "@/lib/utils";

defineProps<{ class?: string }>();
</script>

<template>
  <kbd
    :class="
      cn(
        'inline-flex h-5 min-w-5 items-center justify-center rounded-xs border border-line bg-surface-2 px-1 font-sans text-[11px] font-medium text-ink-3 shadow-[inset_0_-1px_0_var(--line)]',
        $props.class,
      )
    "
  >
    <slot />
  </kbd>
</template>
