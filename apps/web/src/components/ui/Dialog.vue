<script setup lang="ts">
import { X } from "lucide-vue-next";
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogRoot,
  DialogTitle,
} from "reka-ui";

import { cn } from "@/lib/utils";

const props = withDefaults(
  defineProps<{
    title?: string;
    description?: string;
    size?: "sm" | "md" | "lg" | "xl";
    /** When false there is no close button and neither Escape nor the backdrop closes it. */
    dismissable?: boolean;
    class?: string;
    contentClass?: string;
    testId?: string;
  }>(),
  { size: "md", dismissable: true },
);

const open = defineModel<boolean>("open", { default: false });

const widths = { sm: "max-w-[400px]", md: "max-w-[480px]", lg: "max-w-[640px]", xl: "max-w-[820px]" } as const;

function guard(event: Event): void {
  if (!props.dismissable) event.preventDefault();
}
</script>

<template>
  <DialogRoot v-model:open="open">
    <DialogPortal>
      <DialogOverlay
        class="fixed inset-0 z-50 bg-overlay backdrop-blur-[2px] data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 duration-150"
      />
      <DialogContent
        :data-test="testId"
        :class="
          cn(
            'fixed left-1/2 top-1/2 z-50 grid w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 gap-4 rounded-lg border border-line bg-surface p-5 shadow-pop outline-none data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-[0.98] data-[state=closed]:animate-out data-[state=closed]:fade-out-0 duration-150 max-h-[calc(100vh-2rem)] overflow-y-auto',
            widths[size],
            $props.class,
          )
        "
        @escape-key-down="guard"
        @pointer-down-outside="guard"
        @interact-outside="guard"
      >
        <header v-if="title || $slots.header" class="flex items-start justify-between gap-4">
          <div class="min-w-0">
            <slot name="header">
              <DialogTitle class="font-display text-[17px] font-semibold text-ink">{{ title }}</DialogTitle>
              <DialogDescription v-if="description" class="mt-1 text-[13px] leading-relaxed text-ink-2">
                {{ description }}
              </DialogDescription>
            </slot>
          </div>
          <DialogClose
            v-if="dismissable"
            class="-mr-1.5 -mt-1.5 inline-flex size-7 shrink-0 items-center justify-center rounded-sm text-ink-3 transition-colors hover:bg-surface-3 hover:text-ink focus-visible:ring-2 focus-visible:ring-brand/40"
            data-test="dialog-close"
          >
            <X class="size-4" aria-hidden="true" />
          </DialogClose>
        </header>
        <div :class="cn('min-w-0', contentClass)"><slot /></div>
        <footer v-if="$slots.footer" class="flex flex-wrap items-center justify-end gap-2">
          <slot name="footer" />
        </footer>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
