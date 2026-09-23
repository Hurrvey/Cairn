<script setup lang="ts">
/** Voluntary password change from the user menu (POST /v1/auth/change-password). */
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { ApiError } from "@/api/client";
import Button from "@/components/ui/Button.vue";
import Dialog from "@/components/ui/Dialog.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import Notice from "@/components/ui/Notice.vue";
import { describeError } from "@/shared/errors";
import { useToasts } from "@/shared/composables/useToasts";
import { useSessionStore } from "@/stores/session";

const open = defineModel<boolean>("open", { default: false });
const { t } = useI18n();
const session = useSessionStore();
const toasts = useToasts();

const form = reactive({ current: "", next: "", confirm: "" });
const submitting = ref(false);
const error = ref<string | null>(null);
const fieldErrors = ref<Record<string, string[]>>({});

const canSubmit = computed(
  () => !submitting.value && form.current.length > 0 && form.next.length >= 12 && form.next === form.confirm,
);

watch(open, (value) => {
  if (value) {
    Object.assign(form, { current: "", next: "", confirm: "" });
    error.value = null;
    fieldErrors.value = {};
  }
});

async function submit(): Promise<void> {
  if (!canSubmit.value) return;
  submitting.value = true;
  error.value = null;
  fieldErrors.value = {};
  try {
    await session.changePassword(form.current, form.next);
    open.value = false;
    toasts.success(t("shell.passwordChanged"));
  } catch (caught) {
    error.value = describeError(caught).message;
    if (caught instanceof ApiError) fieldErrors.value = caught.byField();
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <Dialog v-model:open="open" :title="t('shell.changePassword')" :description="t('shell.changePasswordHint')" size="sm" test-id="change-password-dialog">
    <form class="grid gap-3" @submit.prevent="submit">
      <Notice v-if="error" tone="bad" test-id="form-error">{{ error }}</Notice>
      <Field :label="t('credentials.currentPassword')" :error="fieldErrors.current_password" v-slot="{ id }">
        <Input :id="id" v-model="form.current" type="password" autocomplete="current-password" data-test="current-password" />
      </Field>
      <Field :label="t('credentials.newPassword')" :error="fieldErrors.new_password" :hint="t('credentials.rule.length', { n: 12 })" v-slot="{ id }">
        <Input :id="id" v-model="form.next" type="password" autocomplete="new-password" data-test="new-password" />
      </Field>
      <Field :label="t('credentials.confirmPassword')" :error="form.confirm && form.confirm !== form.next ? t('credentials.rule.match') : undefined" v-slot="{ id }">
        <Input :id="id" v-model="form.confirm" type="password" autocomplete="new-password" data-test="confirm-password" />
      </Field>
      <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
    </form>
    <template #footer>
      <Button variant="ghost" @click="open = false">{{ t("common.cancel") }}</Button>
      <Button variant="primary" :disabled="!canSubmit" :loading="submitting" data-test="submit" @click="submit">
        {{ t("common.save") }}
      </Button>
    </template>
  </Dialog>
</template>
