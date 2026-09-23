<script setup lang="ts">
/**
 * Section: a titled region separated by a hairline, not a card. Sections
 * stack to form a continuous page; the title row can carry actions.
 */
import { cn } from "@/lib/utils";

defineProps<{ title?: string; description?: string; class?: string; id?: string }>();
</script>

<template>
  <section :id="id" :class="cn('border-t border-line py-5 first:border-t-0 first:pt-0', $props.class)" :aria-labelledby="id ? `${id}-title` : undefined">
    <div v-if="title || $slots.actions" class="mb-3 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
      <div class="min-w-0">
        <h2 v-if="title" :id="id ? `${id}-title` : undefined" class="text-[15px] font-semibold text-ink">{{ title }}</h2>
        <p v-if="description" class="mt-0.5 max-w-[72ch] text-[12.5px] leading-relaxed text-ink-2">{{ description }}</p>
      </div>
      <div v-if="$slots.actions" class="flex shrink-0 flex-wrap items-center gap-2"><slot name="actions" /></div>
    </div>
    <div><slot /></div>
  </section>
</template>
