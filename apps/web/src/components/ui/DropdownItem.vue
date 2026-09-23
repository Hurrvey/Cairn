<script setup lang="ts">
import { DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator } from "reka-ui";

import { cn } from "@/lib/utils";

withDefaults(
  defineProps<{ kind?: "item" | "separator" | "label"; danger?: boolean; disabled?: boolean; class?: string }>(),
  { kind: "item" },
);
const emit = defineEmits<{ select: [event: Event] }>();
</script>

<template>
  <DropdownMenuSeparator v-if="kind === 'separator'" class="my-1 h-px bg-line" />
  <DropdownMenuLabel v-else-if="kind === 'label'" class="px-2 py-1 text-[11px] font-medium uppercase tracking-wide text-ink-3">
    <slot />
  </DropdownMenuLabel>
  <DropdownMenuItem
    v-else
    :disabled="disabled"
    :class="
      cn(
        'flex cursor-default select-none items-center gap-2 rounded-xs px-2 py-1.5 text-[13px] text-ink outline-none data-[highlighted]:bg-surface-3 data-[disabled]:opacity-50 [&_svg]:size-4 [&_svg]:text-ink-3',
        danger && 'text-bad data-[highlighted]:bg-bad-soft [&_svg]:text-bad',
        $props.class,
      )
    "
    @select="emit('select', $event)"
  >
    <slot />
  </DropdownMenuItem>
</template>
