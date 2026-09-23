<script setup lang="ts">
/** Brand mark: three stacked stones, or the deployment's own logo. */
import { BRAND } from "@/app/brand";
import { cn } from "@/lib/utils";

withDefaults(defineProps<{ size?: number; class?: string; wordmark?: boolean }>(), { size: 22, wordmark: false });
</script>

<template>
  <span :class="cn('inline-flex items-center gap-2', $props.class)">
    <img v-if="BRAND.logo" :src="BRAND.logo" :alt="BRAND.name" :height="size" :style="{ height: `${size}px` }" class="w-auto shrink-0" />
    <svg v-else :width="size" :height="size" viewBox="0 0 32 32" aria-hidden="true" class="shrink-0">
      <rect x="3" y="23" width="26" height="6" rx="3" class="fill-brand" />
      <rect x="6.5" y="14.5" width="19" height="6" rx="3" class="fill-brand" opacity="0.82" />
      <rect x="10.5" y="6" width="11" height="6" rx="3" class="fill-brand" opacity="0.62" />
    </svg>
    <span v-if="wordmark && !BRAND.logo" class="font-display text-[17px] font-semibold tracking-tight text-ink">{{ BRAND.name }}</span>
  </span>
</template>
