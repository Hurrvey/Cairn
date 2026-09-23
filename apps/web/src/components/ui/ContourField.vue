<script setup lang="ts">
/**
 * Topographic contour field. Purely decorative: aria-hidden, drifts slowly,
 * and stops entirely under `prefers-reduced-motion`.
 */
import { cn } from "@/lib/utils";

withDefaults(defineProps<{ class?: string; animated?: boolean; opacity?: number }>(), {
  animated: true,
  opacity: 0.55,
});

const PATHS = [
  "M-40 120 C 80 60, 160 200, 300 140 S 520 60, 660 150 S 900 240, 1040 150",
  "M-40 170 C 90 110, 170 250, 310 190 S 530 110, 670 200 S 910 290, 1050 200",
  "M-40 220 C 100 160, 180 300, 320 240 S 540 160, 680 250 S 920 340, 1060 250",
  "M-40 270 C 110 210, 190 350, 330 290 S 550 210, 690 300 S 930 390, 1070 300",
  "M-40 320 C 120 260, 200 400, 340 340 S 560 260, 700 350 S 940 440, 1080 350",
  "M-40 370 C 130 310, 210 450, 350 390 S 570 310, 710 400 S 950 490, 1090 400",
  "M-40 420 C 140 360, 220 500, 360 440 S 580 360, 720 450 S 960 540, 1100 450",
  "M-40 470 C 150 410, 230 550, 370 490 S 590 410, 730 500 S 970 590, 1110 500",
  "M-40 520 C 160 460, 240 600, 380 540 S 600 460, 740 550 S 980 640, 1120 550",
  "M-40 570 C 170 510, 250 650, 390 590 S 610 510, 750 600 S 990 690, 1130 600",
  "M-40 70 C 70 10, 150 150, 290 90 S 510 10, 650 100 S 890 190, 1030 100",
  "M-40 20 C 60 -40, 140 100, 280 40 S 500 -40, 640 50 S 880 140, 1020 50",
];
</script>

<template>
  <div :class="cn('pointer-events-none absolute inset-0 overflow-hidden', $props.class)" aria-hidden="true">
    <svg
      :class="cn('absolute -left-40 -top-24 h-[160%] w-[180%] text-brand', animated && 'contour-drift')"
      viewBox="0 0 1000 600"
      preserveAspectRatio="none"
      fill="none"
      :style="{ opacity }"
    >
      <path v-for="(d, index) in PATHS" :key="index" :d="d" stroke="currentColor" :stroke-width="index % 3 === 0 ? 1.1 : 0.6" :stroke-opacity="index % 3 === 0 ? 0.5 : 0.28" />
    </svg>
  </div>
</template>
