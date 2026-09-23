<script setup lang="ts">
/**
 * Form field: label, control, hint and error in one vertical rhythm. Pass an
 * `id` so the label targets the control; the slot receives it back.
 */
import { computed, useId } from "vue";

import { cn } from "@/lib/utils";

const props = defineProps<{
  label?: string;
  hint?: string;
  error?: string | string[] | undefined;
  required?: boolean;
  id?: string;
  class?: string;
  inline?: boolean;
}>();

const message = computed(() => (Array.isArray(props.error) ? props.error[0] : props.error));
const generated = useId();
const fieldId = computed(() => props.id ?? generated);
</script>

<template>
  <div :class="cn('grid gap-1.5', inline && 'grid-cols-[minmax(0,1fr)_auto] items-center', $props.class)">
    <label v-if="label" :for="fieldId" class="text-[12.5px] font-medium text-ink-2">
      {{ label }}<span v-if="required" class="ml-0.5 text-bad" aria-hidden="true">*</span>
    </label>
    <slot :id="fieldId" />
    <p v-if="message" class="text-[12px] leading-snug text-bad" role="alert">{{ message }}</p>
    <p v-else-if="hint" class="text-[12px] leading-snug text-ink-3">{{ hint }}</p>
  </div>
</template>
