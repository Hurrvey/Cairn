<script setup lang="ts">
/**
 * Page header: an optional back link, the title, a line of metadata and the
 * primary actions. Titles are set in the display face; everything else stays
 * quiet so the content below carries the weight.
 */
import { ArrowLeft } from "lucide-vue-next";
import type { RouteLocationRaw } from "vue-router";

import { cn } from "@/lib/utils";

defineProps<{
  title: string;
  description?: string;
  back?: RouteLocationRaw;
  backLabel?: string;
  class?: string;
}>();
</script>

<template>
  <header :class="cn('mb-5 flex flex-wrap items-end justify-between gap-x-6 gap-y-3', $props.class)">
    <div class="min-w-0">
      <RouterLink
        v-if="back"
        :to="back"
        class="mb-1.5 inline-flex items-center gap-1 text-[12.5px] text-ink-3 transition-colors hover:text-ink"
        data-test="back-link"
      >
        <ArrowLeft class="size-3.5" aria-hidden="true" />
        {{ backLabel }}
      </RouterLink>
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h1 class="min-w-0 truncate text-[22px] font-semibold leading-tight text-ink" data-test="page-title">{{ title }}</h1>
        <slot name="badges" />
      </div>
      <p v-if="description" class="mt-1 max-w-[70ch] text-[13px] leading-relaxed text-ink-2">{{ description }}</p>
      <slot name="meta" />
    </div>
    <div v-if="$slots.actions" class="flex flex-wrap items-center gap-2"><slot name="actions" /></div>
  </header>
</template>
