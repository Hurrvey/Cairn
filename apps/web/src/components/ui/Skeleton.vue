<script setup lang="ts">
import { cn } from "@/lib/utils";

withDefaults(defineProps<{ rows?: number; class?: string; lines?: number[] }>(), { rows: 4 });
</script>

<template>
  <div :class="cn('grid gap-3', $props.class)" aria-busy="true" aria-live="polite">
    <div v-for="row in rows" :key="row" class="flex items-center gap-3">
      <span class="skeleton h-3.5" :style="{ width: `${lines?.[(row - 1) % (lines?.length || 1)] ?? 40 + ((row * 17) % 45)}%` }" />
      <span class="skeleton h-3.5 w-14" />
    </div>
  </div>
</template>
