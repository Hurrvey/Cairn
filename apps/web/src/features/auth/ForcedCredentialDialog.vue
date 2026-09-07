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
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { ApiError } from "@/api/client";
import { useSessionStore } from "@/stores/session";

const { t } = useI18n();
const session = useSessionStore();

const form = reactive({
  username: "",
  currentPassword: "",
  newPassword: "",
  confirmPassword: "",
});

const submitting = ref(false);
const formError = ref<string | null>(null);
const fieldErrors = ref<Record<string, string[]>>({});

const visible = computed(() => session.mustChangePassword);
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
  () =>
    !submitting.value &&
    form.currentPassword.length > 0 &&
    rules.value.every((rule) => rule.ok),
);

// --- submit ------------------------------------------------------------------

async function submit(): Promise<void> {
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
  <el-dialog
    :model-value="visible"
    :title="t('credentials.title')"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    :destroy-on-close="false"
    align-center
    width="480px"
    class="credential-dialog"
    data-test="forced-credential-dialog"
  >
    <p class="lede">
      {{
        session.changeReason === "admin_forced"
          ? t("credentials.ledeAdminForced")
          : t("credentials.ledeInitial")
      }}
    </p>

    <el-alert
      v-if="formError"
      :title="formError"
      type="error"
      show-icon
      :closable="false"
      data-test="form-error"
      class="mb"
    />

    <el-form label-position="top" @submit.prevent="submit">
      <el-form-item
        v-if="usernameEditable"
        :label="t('credentials.username')"
        :error="fieldErrors.new_username?.[0]"
      >
        <el-input
          v-model="form.username"
          autocomplete="username"
          data-test="username"
          :placeholder="session.user?.username"
        />
        <span class="hint">{{ t("credentials.usernameHint") }}</span>
      </el-form-item>

      <el-form-item
        :label="t('credentials.currentPassword')"
        :error="fieldErrors.current_password?.[0]"
      >
        <el-input
          v-model="form.currentPassword"
          type="password"
          show-password
          autocomplete="current-password"
          data-test="current-password"
        />
      </el-form-item>

      <el-form-item :label="t('credentials.newPassword')" :error="fieldErrors.new_password?.[0]">
        <el-input
          v-model="form.newPassword"
          type="password"
          show-password
          autocomplete="new-password"
          data-test="new-password"
        />
      </el-form-item>

      <el-form-item :label="t('credentials.confirmPassword')">
        <el-input
          v-model="form.confirmPassword"
          type="password"
          show-password
          autocomplete="new-password"
          data-test="confirm-password"
          @keyup.enter="canSubmit && submit()"
        />
      </el-form-item>
    </el-form>

    <ul v-if="rules.length" class="rules" data-test="policy-rules">
      <li v-for="rule in rules" :key="rule.key" :class="{ ok: rule.ok }" :data-rule="rule.key">
        <span aria-hidden="true">{{ rule.ok ? "✓" : "○" }}</span>
        <span>{{ rule.label }}</span>
      </li>
    </ul>

    <template #footer>
      <el-button link data-test="sign-out" @click="signOut">
        {{ t("credentials.signOut") }}
      </el-button>
      <el-button
        type="primary"
        :disabled="!canSubmit"
        :loading="submitting"
        data-test="submit"
        @click="submit"
      >
        {{ t("credentials.submit") }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.lede {
  margin: 0 0 var(--cairn-space-4);
  color: var(--cairn-text-muted);
  line-height: 1.55;
}
.mb {
  margin-bottom: var(--cairn-space-4);
}
.hint {
  font-size: 12px;
  color: var(--cairn-text-muted);
}
.rules {
  list-style: none;
  margin: var(--cairn-space-2) 0 0;
  padding: var(--cairn-space-3);
  background: var(--cairn-surface-sunken);
  border-radius: 6px;
  font-size: 13px;
}
.rules li {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 3px 0;
  color: var(--cairn-text-muted);
}
.rules li.ok {
  color: var(--cairn-success);
}
</style>
