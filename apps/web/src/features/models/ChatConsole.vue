<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { streamChat, type ChatEvent } from "@/api/model-chat";
import type { Model } from "@/api/setup";
import Button from "@/components/ui/Button.vue";
import Sheet from "@/components/ui/Sheet.vue";
import Notice from "@/components/ui/Notice.vue";
import Textarea from "@/components/ui/Textarea.vue";

const props = defineProps<{ model: Model | null }>();
const open = defineModel<boolean>("open", { default: false });
const { t } = useI18n();
const prompt = ref("");
const output = ref("");
const error = ref("");
const busy = ref(false);
const usage = ref<ChatEvent | null>(null);
let controller: AbortController | null = null;
function cancel() { controller?.abort(); }
watch(open, (value) => { if (!value) cancel(); });
onBeforeUnmount(cancel);
async function send() {
  if (!props.model || !prompt.value.trim()) return;
  cancel(); controller = new AbortController();
  busy.value = true; output.value = ""; error.value = ""; usage.value = null;
  try {
    await streamChat(props.model.id, prompt.value, (event) => {
      if (event.type === "delta") output.value += event.delta || "";
      if (event.type === "completed") usage.value = event;
    }, controller.signal);
  } catch (caught) {
    error.value = caught instanceof DOMException && caught.name === "AbortError" ? t("models.chat.cancelled") : t("models.chat.failed");
  } finally { busy.value = false; }
}
</script>

<template>
  <Sheet v-model:open="open" :title="t('models.chat.title', { name: model?.display_name || '' })" :description="t('models.chat.hint')" test-id="chat-console">
    <form class="grid gap-3" @submit.prevent="send">
      <label class="text-sm" for="chat-prompt">{{ t('models.chat.prompt') }}</label>
      <Textarea id="chat-prompt" v-model="prompt" :rows="4" :disabled="busy" data-test="chat-prompt" />
      <Notice v-if="error" tone="bad">{{ error }}</Notice>
      <div class="min-h-32 whitespace-pre-wrap rounded-md border border-line bg-surface p-3 text-sm" aria-live="polite" data-test="chat-output">{{ output || t('models.chat.empty') }}</div>
      <p v-if="usage" class="text-xs text-ink-3" data-test="chat-usage">{{ t('models.chat.tokens', { input: usage.input_tokens ?? '—', output: usage.output_tokens ?? '—' }) }}</p>
    </form>
    <template #footer>
      <Button v-if="busy" variant="secondary" @click="cancel">{{ t('models.chat.cancel') }}</Button>
      <Button variant="primary" :disabled="busy || !prompt.trim()" @click="send" data-test="chat-send">{{ t('models.chat.send') }}</Button>
    </template>
  </Sheet>
</template>
