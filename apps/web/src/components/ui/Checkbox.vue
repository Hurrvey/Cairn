<script setup lang="ts">
import { Check, Minus } from "lucide-vue-next";
import { CheckboxIndicator, CheckboxRoot } from "reka-ui";

import { cn } from "@/lib/utils";

defineProps<{ disabled?: boolean; class?: string; id?: string; name?: string; value?: string }>();
const model = defineModel<boolean | "indeterminate">({ default: false });
</script>

<template>
  <label :class="cn('inline-flex cursor-pointer items-start gap-2 text-[13px] text-ink', disabled && 'cursor-not-allowed opacity-60', $props.class)">
    <CheckboxRoot
      v-model="model"
      :id="id"
      :name="name"
      :value="value"
      :disabled="disabled"
      class="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-xs border border-line-strong bg-surface outline-none transition-colors focus-visible:ring-2 focus-visible:ring-brand/40 data-[state=checked]:border-brand data-[state=checked]:bg-brand data-[state=indeterminate]:border-brand data-[state=indeterminate]:bg-brand"
    >
      <CheckboxIndicator class="text-brand-ink">
        <Minus v-if="model === 'indeterminate'" class="size-3" stroke-width="3" />
        <Check v-else class="size-3" stroke-width="3" />
      </CheckboxIndicator>
    </CheckboxRoot>
    <span v-if="$slots.default" class="leading-snug"><slot /></span>
  </label>
</template>
