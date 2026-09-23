<script setup lang="ts">
import { SwitchRoot, SwitchThumb } from "reka-ui";

import { cn } from "@/lib/utils";

defineProps<{ disabled?: boolean; class?: string; id?: string; label?: string }>();
const model = defineModel<boolean>({ default: false });
</script>

<template>
  <label :class="cn('inline-flex items-center gap-2 text-[13px] text-ink', disabled && 'opacity-60', $props.class)">
    <SwitchRoot
      v-model="model"
      :id="id"
      :disabled="disabled"
      class="relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-transparent bg-line-strong transition-colors outline-none focus-visible:ring-2 focus-visible:ring-brand/40 data-[state=checked]:bg-brand disabled:cursor-not-allowed"
    >
      <SwitchThumb
        class="block size-4 translate-x-0.5 rounded-full bg-white shadow-[0_1px_2px_rgb(0_0_0/0.3)] transition-transform duration-150 data-[state=checked]:translate-x-[18px]"
      />
    </SwitchRoot>
    <span v-if="label || $slots.default"><slot>{{ label }}</slot></span>
  </label>
</template>
