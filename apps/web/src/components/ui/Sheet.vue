<script setup lang="ts">
/**
 * Side sheet: the home for create/edit forms and secondary detail. Keeps the
 * list behind it visible so the user never loses their place.
 */
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

withDefaults(
  defineProps<{
    title?: string;
    description?: string;
    size?: "sm" | "md" | "lg" | "xl";
    side?: "right" | "left";
    testId?: string;
    class?: string;
  }>(),
  { size: "md", side: "right" },
);

const open = defineModel<boolean>("open", { default: false });
const widths = { sm: "sm:max-w-[320px]", md: "sm:max-w-[480px]", lg: "sm:max-w-[640px]", xl: "sm:max-w-[860px]" } as const;
const sides = {
  right: "right-0 border-l data-[state=open]:slide-in-from-right data-[state=closed]:slide-out-to-right",
  left: "left-0 border-r data-[state=open]:slide-in-from-left data-[state=closed]:slide-out-to-left",
} as const;
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
            'fixed inset-y-0 z-50 flex w-full flex-col border-line bg-surface shadow-sheet outline-none data-[state=open]:animate-in data-[state=closed]:animate-out duration-200',
            sides[side],
            widths[size],
            $props.class,
          )
        "
      >
        <header class="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div class="min-w-0">
            <DialogTitle class="font-display text-[17px] font-semibold text-ink">{{ title }}</DialogTitle>
            <DialogDescription v-if="description" class="mt-1 text-[13px] leading-relaxed text-ink-2">
              {{ description }}
            </DialogDescription>
            <slot name="header" />
          </div>
          <DialogClose
            class="-mr-1.5 -mt-1 inline-flex size-7 shrink-0 items-center justify-center rounded-sm text-ink-3 transition-colors hover:bg-surface-3 hover:text-ink focus-visible:ring-2 focus-visible:ring-brand/40"
            data-test="sheet-close"
          >
            <X class="size-4" aria-hidden="true" />
          </DialogClose>
        </header>
        <div class="min-h-0 flex-1 overflow-y-auto px-5 py-4"><slot /></div>
        <footer v-if="$slots.footer" class="flex flex-wrap items-center justify-end gap-2 border-t border-line px-5 py-3">
          <slot name="footer" />
        </footer>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
