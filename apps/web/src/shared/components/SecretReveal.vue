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
  <div class="secret" data-test="secret-reveal">
    <el-alert type="warning" :closable="false" show-icon class="warn">
      <template #title>{{ t("secret.onceTitle") }}</template>
      {{ hint ?? t("secret.onceBody") }}
    </el-alert>

    <label>{{ label }}</label>
    <div class="row">
      <code data-test="secret-value">{{ value }}</code>
      <el-button size="small" data-test="secret-copy" @click="copy">
        {{ copied ? t("secret.copied") : t("secret.copy") }}
      </el-button>
    </div>

    <el-checkbox v-model="acknowledged" data-test="secret-ack">
      {{ t("secret.acknowledge") }}
    </el-checkbox>
  </div>
</template>

<style scoped>
.secret {
  display: grid;
  gap: var(--cairn-space-3);
}
.warn {
  margin-bottom: 0;
}
label {
  font-size: 12px;
  color: var(--cairn-text-muted);
}
.row {
  display: flex;
  gap: var(--cairn-space-2);
  align-items: center;
}
code {
  flex: 1;
  padding: 10px 12px;
  background: var(--cairn-surface-sunken);
  border: 1px solid var(--cairn-border);
  border-radius: 6px;
  font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
  font-size: 13px;
  word-break: break-all;
  user-select: all;
}
</style>
