<script setup lang="ts">
/**
 * Mandatory credential change (FR-P-02, FR-A-07).
 *
 * Non-dismissable by construction: no close button, no ESC, no backdrop click.
 * That is a product requirement, not a security control — the server already
 * refuses every non-allowlisted route for this principal. What this dialog
 * guarantees is that the user is never staring at a half-usable application
 * wondering why nothing works.
 *
 * The policy rules rendered below come from the server's `policy` object. They
 * are deliberately NOT hardcoded: a deployment that raises `min_length` must see
 * the new rule here without a frontend release, and a client that disagreed with
 * the server would be worse than no hint at all.
 */
import { Check, Circle } from "lucide-vue-next";
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { ApiError } from "@/api/client";
import Button from "@/components/ui/Button.vue";
import Dialog from "@/components/ui/Dialog.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import Notice from "@/components/ui/Notice.vue";
import { useSessionStore } from "@/stores/session";

const { t } = useI18n();
const session = useSessionStore();
const emit = defineEmits<{ completed: [] }>();

const form = reactive({
  username: "",
  currentPassword: "",
  newPassword: "",
  confirmPassword: "",
});

const submitting = ref(false);
const formError = ref<string | null>(null);
const fieldErrors = ref<Record<string, string[]>>({});

const visible = computed({
  get: () => session.mustChangePassword,
  // The dialog cannot be closed from the UI; only the store changes this.
  set: () => undefined,
});
const policy = computed(() => session.policy);
const usernameEditable = computed(() => policy.value?.username_editable ?? true);

watch(
  () => session.user?.username,
  (value) => {
    if (value && !form.username) form.username = value;
  },
  { immediate: true },
);

// --- live policy checks ------------------------------------------------------

interface Rule {
  key: string;
  label: string;
  ok: boolean;
}

const CLASS_PATTERNS: Record<string, RegExp> = {
  lowercase: /[a-z]/,
  uppercase: /[A-Z]/,
  digit: /[0-9]/,
  symbol: /[^A-Za-z0-9]/,
};

const matchedClasses = computed(() => {
  const classes = policy.value?.classes ?? [];
  return classes.filter((name) => CLASS_PATTERNS[name]?.test(form.newPassword)).length;
});

const rules = computed<Rule[]>(() => {
  const p = policy.value;
  if (!p) return [];
  return [
    {
      key: "length",
      label: t("credentials.rule.length", { n: p.min_length }),
      ok: form.newPassword.length >= p.min_length,
    },
    {
      key: "classes",
      label: t("credentials.rule.classes", { n: p.require_classes }),
      ok: matchedClasses.value >= p.require_classes,
    },
    {
      key: "username",
      label: t("credentials.rule.username"),
      ok:
        form.username.length < 3 ||
        !form.newPassword.toLowerCase().includes(form.username.toLowerCase()),
    },
    {
      key: "match",
      label: t("credentials.rule.match"),
      ok: form.newPassword.length > 0 && form.newPassword === form.confirmPassword,
    },
  ];
});

const canSubmit = computed(
  () => !submitting.value && form.currentPassword.length > 0 && rules.value.every((rule) => rule.ok),
);

// --- submit ------------------------------------------------------------------

async function submit(): Promise<void> {
  if (!canSubmit.value) return;
  formError.value = null;
  fieldErrors.value = {};
  submitting.value = true;
  try {
    const renamed =
      usernameEditable.value && form.username && form.username !== session.user?.username;
    await session.completeSetup({
      currentPassword: form.currentPassword,
      newPassword: form.newPassword,
      confirmPassword: form.confirmPassword,
      newUsername: renamed ? form.username : undefined,
    });
    emit("completed");
  } catch (error) {
    if (error instanceof ApiError) {
      formError.value = error.detail;
      fieldErrors.value = error.byField();
      // A taken username rolls the whole change back server-side, so the user
      // keeps the password they just typed — re-entering it would be pointless
      // busywork. Only the username needs attention.
      if (error.code === "USERNAME_TAKEN") {
        fieldErrors.value = { new_username: [error.detail] };
      }
    } else {
      formError.value = t("errors.network");
    }
  } finally {
    submitting.value = false;
  }
}

async function signOut(): Promise<void> {
  await session.signOut();
}
</script>

<template>
  <Dialog
    v-model:open="visible"
    :title="t('credentials.title')"
    :description="session.changeReason === 'admin_forced' ? t('credentials.ledeAdminForced') : t('credentials.ledeInitial')"
    :dismissable="false"
    size="md"
    test-id="forced-credential-dialog"
  >
    <form class="grid gap-3.5" @submit.prevent="submit">
      <Notice v-if="formError" tone="bad" test-id="form-error">{{ formError }}</Notice>

      <Field
        v-if="usernameEditable"
        :label="t('credentials.username')"
        :hint="t('credentials.usernameHint')"
        :error="fieldErrors.new_username"
        v-slot="{ id }"
      >
        <Input :id="id" v-model="form.username" autocomplete="username" data-test="username" :placeholder="session.user?.username" />
      </Field>

      <Field :label="t('credentials.currentPassword')" :error="fieldErrors.current_password" v-slot="{ id }">
        <Input :id="id" v-model="form.currentPassword" type="password" autocomplete="current-password" data-test="current-password" />
      </Field>

      <Field :label="t('credentials.newPassword')" :error="fieldErrors.new_password" v-slot="{ id }">
        <Input :id="id" v-model="form.newPassword" type="password" autocomplete="new-password" data-test="new-password" />
      </Field>

      <Field :label="t('credentials.confirmPassword')" v-slot="{ id }">
        <Input :id="id" v-model="form.confirmPassword" type="password" autocomplete="new-password" data-test="confirm-password" />
      </Field>

      <ul v-if="rules.length" class="grid gap-1 rounded-md bg-surface-2 px-3 py-2.5 text-[12.5px]" data-test="policy-rules">
        <li
          v-for="rule in rules"
          :key="rule.key"
          :class="['flex items-center gap-2 transition-colors', rule.ok ? 'ok text-ok' : 'text-ink-3']"
          :data-rule="rule.key"
        >
          <Check v-if="rule.ok" class="size-3.5" aria-hidden="true" />
          <Circle v-else class="size-3.5" aria-hidden="true" />
          <span>{{ rule.label }}</span>
        </li>
      </ul>
      <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
    </form>

    <template #footer>
      <Button variant="ghost" data-test="sign-out" @click="signOut">{{ t("credentials.signOut") }}</Button>
      <Button variant="primary" :disabled="!canSubmit" :loading="submitting" data-test="submit" @click="submit">
        {{ t("credentials.submit") }}
      </Button>
    </template>
  </Dialog>
</template>
