<script setup lang="ts">
import { TooltipContent, TooltipPortal, TooltipRoot, TooltipTrigger } from "reka-ui";

withDefaults(defineProps<{ text: string; side?: "top" | "bottom" | "left" | "right" }>(), { side: "top" });
</script>

<template>
  <TooltipRoot :delay-duration="300">
    <TooltipTrigger as-child>
      <slot />
    </TooltipTrigger>
    <TooltipPortal>
      <TooltipContent
        :side="side"
        :side-offset="6"
        class="z-50 max-w-72 rounded-sm border border-line bg-ink px-2 py-1 text-[12px] leading-snug text-bg shadow-pop data-[state=delayed-open]:animate-in data-[state=delayed-open]:fade-in-0 duration-100 dark:bg-surface-3 dark:text-ink"
      >
        {{ text }}
      </TooltipContent>
    </TooltipPortal>
  </TooltipRoot>
</template>
