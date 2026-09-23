<script setup lang="ts">
import { Check, Copy } from "lucide-vue-next";
import { ref } from "vue";
import { useI18n } from "vue-i18n";

import Button from "@/components/ui/Button.vue";

const props = withDefaults(defineProps<{ value: string; label?: string; size?: "sm" | "icon-sm" | "md"; variant?: "ghost" | "outline" | "secondary" }>(), {
  size: "icon-sm",
  variant: "ghost",
});
const { t } = useI18n();
const copied = ref(false);
const emit = defineEmits<{ copied: [] }>();

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.value);
  } catch {
    // No clipboard over plain http or without permission; the value stays
    // selectable, so the user copies by hand.
    return;
  }
  copied.value = true;
  emit("copied");
  window.setTimeout(() => (copied.value = false), 1800);
}
</script>

<template>
  <Button :size="size" :variant="variant" :aria-label="label ?? t('common.copy')" :title="label ?? t('common.copy')" data-test="copy" @click="copy">
    <Check v-if="copied" class="text-ok" aria-hidden="true" />
    <Copy v-else aria-hidden="true" />
    <span v-if="size !== 'icon-sm'">{{ copied ? t("common.copied") : (label ?? t("common.copy")) }}</span>
  </Button>
</template>
