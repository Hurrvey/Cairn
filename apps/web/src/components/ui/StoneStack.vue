<script setup lang="ts">
/**
 * The stone stack: six stones for the six pipeline stages a document passes
 * through. Filled stones are done, the top stone glows while a stage runs,
 * and a failed document shows its stones tipped. Small enough to sit inside a
 * table row, distinct enough to read from across the room.
 */
import { computed } from "vue";

import { cn } from "@/lib/utils";
import { stageOf } from "@/shared/pipeline";

const props = withDefaults(defineProps<{ state: string; size?: number; class?: string; title?: string }>(), {
  size: 18,
});

const info = computed(() => stageOf(props.state));

// Stones from bottom (widest) to top (narrowest); x/y in a 24x24 box.
const STONES = [
  { w: 20, h: 3.6, y: 20 },
  { w: 17, h: 3.4, y: 16.4 },
  { w: 14.5, h: 3.2, y: 13 },
  { w: 12, h: 3, y: 9.8 },
  { w: 9.5, h: 2.8, y: 6.8 },
  { w: 7, h: 2.6, y: 4 },
];

function stoneClass(index: number): string {
  if (info.value.failed) return index < 2 ? "fill-bad/70" : "fill-bad/25";
  if (info.value.deleting) return "fill-warn/40";
  if (index < info.value.filled) return "fill-brand";
  if (index === info.value.filled && info.value.active) return "fill-brand/45 stone-settle";
  return "fill-line-strong/60";
}
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    :class="cn('shrink-0 overflow-visible', $props.class)"
    role="img"
    :aria-label="title ?? state"
  >
    <title v-if="title">{{ title }}</title>
    <g :transform="info.failed ? 'rotate(-9 12 22)' : undefined">
      <rect
        v-for="(stone, index) in STONES"
        :key="index"
        :x="12 - stone.w / 2"
        :y="stone.y"
        :width="stone.w"
        :height="stone.h"
        :rx="stone.h / 2"
        :class="cn('transition-[fill] duration-300', stoneClass(index))"
      />
    </g>
  </svg>
</template>
