<script setup lang="ts">
/**
 * Show-once secret.
 *
 * Used for the generated initial password and the API key plaintext. Both are
 * unrecoverable, so the component's job is to make that impossible to miss and
 * to make copying frictionless — a user who fails to save it has to reissue,
 * and a user who is nagged about it copies it before closing.
 */
import { ref } from "vue";
import { useI18n } from "vue-i18n";

import Button from "@/components/ui/Button.vue";
import Checkbox from "@/components/ui/Checkbox.vue";
import Notice from "@/components/ui/Notice.vue";

const props = defineProps<{
  value: string;
  label: string;
  /** Extra sentence explaining what happens if it is lost. */
  hint?: string;
}>();

const { t } = useI18n();
const copied = ref(false);
const acknowledged = defineModel<boolean>("acknowledged", { default: false });

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.value);
  } catch {
    // Clipboard API needs a secure context; over plain http the user copies by
    // hand. Failing silently here would be worse than showing nothing changed.
    return;
  }
  copied.value = true;
  window.setTimeout(() => (copied.value = false), 2000);
}
</script>

<template>
  <div class="grid gap-3" data-test="secret-reveal">
    <Notice tone="warn" :title="t('secret.onceTitle')">{{ hint ?? t("secret.onceBody") }}</Notice>

    <div>
      <p class="mb-1 text-[12px] font-medium text-ink-2">{{ label }}</p>
      <div class="flex items-stretch gap-2">
        <code
          class="min-w-0 flex-1 select-all break-all rounded-sm border border-line bg-surface-2 px-3 py-2 font-mono text-[13px] leading-relaxed text-ink"
          data-test="secret-value"
          >{{ value }}</code
        >
        <Button variant="secondary" class="shrink-0 self-center" data-test="secret-copy" @click="copy">
          {{ copied ? t("secret.copied") : t("secret.copy") }}
        </Button>
      </div>
    </div>

    <Checkbox v-model="acknowledged" data-test="secret-ack">{{ t("secret.acknowledge") }}</Checkbox>
  </div>
</template>
