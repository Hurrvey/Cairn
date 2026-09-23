<script setup lang="ts">
/** Definition list laid out as a compact key/value grid. */
import { cn } from "@/lib/utils";

defineProps<{ items: { label: string; value: string | number | null | undefined; mono?: boolean; testId?: string }[]; columns?: 1 | 2 | 3 | 4; class?: string }>();
</script>

<template>
  <dl
    :class="
      cn(
        'grid gap-x-6 gap-y-3',
        columns === 1 ? 'grid-cols-1' : columns === 2 ? 'grid-cols-1 sm:grid-cols-2' : columns === 4 ? 'grid-cols-2 lg:grid-cols-4' : 'grid-cols-2 lg:grid-cols-3',
        $props.class,
      )
    "
  >
    <div v-for="item in items" :key="item.label" class="min-w-0" :data-test="item.testId">
      <dt class="text-[11.5px] font-medium uppercase tracking-wide text-ink-3">{{ item.label }}</dt>
      <dd :class="cn('mt-0.5 truncate text-[13px] text-ink', item.mono && 'font-mono text-[12.5px]')" :title="item.value === null || item.value === undefined ? undefined : String(item.value)">
        {{ item.value === null || item.value === undefined || item.value === "" ? "—" : item.value }}
      </dd>
    </div>
  </dl>
</template>
