<script setup lang="ts">
import { cva } from "class-variance-authority";
import { LoaderCircle } from "lucide-vue-next";
import { computed } from "vue";
import type { RouteLocationRaw } from "vue-router";

import { cn } from "@/lib/utils";

const props = withDefaults(
  defineProps<{
    variant?: "primary" | "secondary" | "outline" | "ghost" | "danger" | "link";
    size?: "sm" | "md" | "lg" | "icon" | "icon-sm";
    loading?: boolean;
    disabled?: boolean;
    type?: "button" | "submit" | "reset";
    to?: RouteLocationRaw;
    href?: string;
    block?: boolean;
    class?: string;
  }>(),
  { variant: "secondary", size: "md", type: "button" },
);

const styles = cva(
  "inline-flex select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-sm font-medium transition-[background-color,border-color,color,box-shadow] duration-150 outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-1 focus-visible:ring-offset-surface disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary: "bg-brand text-brand-ink shadow-[inset_0_-1px_0_rgb(0_0_0/0.12)] hover:bg-brand-strong",
        secondary:
          "bg-surface text-ink border border-line-strong shadow-[0_1px_1px_rgb(26_27_25/0.04)] hover:bg-surface-2 hover:border-ink-3",
        outline: "border border-line text-ink-2 hover:text-ink hover:bg-surface-2",
        ghost: "text-ink-2 hover:bg-surface-3 hover:text-ink",
        danger: "bg-bad text-white hover:brightness-95",
        link: "text-brand underline-offset-4 hover:underline px-0 h-auto",
      },
      size: {
        sm: "h-7 px-2.5 text-[12.5px]",
        md: "h-8 px-3 text-[13px]",
        lg: "h-10 px-4 text-sm",
        icon: "h-8 w-8",
        "icon-sm": "h-7 w-7",
      },
      block: { true: "w-full" },
    },
  },
);

const classes = computed(() =>
  cn(styles({ variant: props.variant, size: props.size, block: props.block ?? false }), props.class),
);
const tag = computed(() => (props.to ? "RouterLink" : props.href ? "a" : "button"));
</script>

<template>
  <component
    :is="tag"
    :to="to"
    :href="href"
    :type="tag === 'button' ? type : undefined"
    :class="classes"
    :disabled="tag === 'button' ? disabled || loading : undefined"
    :aria-disabled="tag !== 'button' && disabled ? 'true' : undefined"
    :aria-busy="loading ? 'true' : undefined"
  >
    <LoaderCircle v-if="loading" class="animate-spin" aria-hidden="true" />
    <slot />
  </component>
</template>
