<script setup lang="ts">
/**
 * Break-glass access: an administrator opens a time-limited, audited window
 * to read a knowledge base they manage but were not granted content access to.
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { knowledge } from "@/api/knowledge";
import Button from "@/components/ui/Button.vue";
import Dialog from "@/components/ui/Dialog.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import Notice from "@/components/ui/Notice.vue";
import Textarea from "@/components/ui/Textarea.vue";
import { describeError } from "@/shared/errors";

const props = defineProps<{ kbId: string }>();
const emit = defineEmits<{ granted: [] }>();
const open = defineModel<boolean>("open", { default: false });
const { t } = useI18n();

const reason = ref("");
const ttl = ref<number | null>(null);
const submitting = ref(false);
const error = ref<string | null>(null);
const canSubmit = computed(() => !submitting.value && reason.value.trim().length >= 10);

watch(open, (value) => {
  if (value) {
    reason.value = "";
    ttl.value = null;
    error.value = null;
  }
});

async function submit(): Promise<void> {
  if (!canSubmit.value) return;
  submitting.value = true;
  error.value = null;
  try {
    await knowledge.breakGlass(props.kbId, reason.value.trim(), ttl.value);
    emit("granted");
  } catch (caught) {
    error.value = describeError(caught).message;
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <Dialog v-model:open="open" :title="t('knowledge.access.dialogTitle')" :description="t('knowledge.access.dialogBody')" test-id="break-glass-dialog">
    <form class="grid gap-3" @submit.prevent="submit">
      <Notice v-if="error" tone="bad">{{ error }}</Notice>
      <Field :label="t('knowledge.access.reason')" :hint="t('knowledge.access.reasonHint')" required v-slot="{ id }">
        <Textarea :id="id" v-model="reason" rows="3" data-test="break-glass-reason" />
      </Field>
      <Field :label="t('knowledge.access.ttl')" :hint="t('knowledge.access.ttlHint')" v-slot="{ id }">
        <Input :id="id" v-model="ttl" type="number" min="5" max="1440" class="max-w-40" />
      </Field>
    </form>
    <template #footer>
      <Button variant="ghost" @click="open = false">{{ t("common.cancel") }}</Button>
      <Button variant="primary" :disabled="!canSubmit" :loading="submitting" data-test="break-glass-submit" @click="submit">{{ t("knowledge.access.open") }}</Button>
    </template>
  </Dialog>
</template>
