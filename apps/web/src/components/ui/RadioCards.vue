<script setup lang="ts">
import { RadioGroupIndicator, RadioGroupItem, RadioGroupRoot } from "reka-ui";

import { cn } from "@/lib/utils";

export interface RadioOption<V extends string> {
  value: V;
  label: string;
  description?: string;
  recommended?: boolean;
}

defineProps<{ options: RadioOption<string>[]; disabled?: boolean; class?: string; name?: string; testId?: string }>();
const model = defineModel<string>({ required: true });
</script>

<template>
  <RadioGroupRoot v-model="model" :disabled="disabled" :name="name" :class="cn('grid gap-2', $props.class)" :data-test="testId">
    <label
      v-for="option in options"
      :key="option.value"
      :class="
        cn(
          'flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 transition-colors',
          model === option.value ? 'border-brand/50 bg-brand-soft/60' : 'border-line hover:border-line-strong',
          disabled && 'cursor-not-allowed opacity-60',
        )
      "
    >
      <RadioGroupItem
        :value="option.value"
        class="mt-1 flex size-4 shrink-0 items-center justify-center rounded-full border border-line-strong bg-surface outline-none focus-visible:ring-2 focus-visible:ring-brand/40 data-[state=checked]:border-brand"
      >
        <RadioGroupIndicator class="size-2 rounded-full bg-brand" />
      </RadioGroupItem>
      <span class="min-w-0">
        <span class="block text-[13px] font-medium text-ink">
          {{ option.label }}
          <span v-if="option.recommended" class="ml-1 rounded-full bg-brand-soft px-1.5 py-px text-[10.5px] font-medium text-brand-strong">{{ $t("common.recommended") }}</span>
        </span>
        <span v-if="option.description" class="mt-0.5 block text-[12px] leading-relaxed text-ink-2">{{ option.description }}</span>
      </span>
    </label>
  </RadioGroupRoot>
</template>
