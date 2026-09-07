<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";

import { ApiError, NetworkError } from "@/api/client";
import ForcedCredentialDialog from "@/features/auth/ForcedCredentialDialog.vue";
import { useSessionStore } from "@/stores/session";

const { t } = useI18n();
const session = useSessionStore();
const router = useRouter();
const route = useRoute();

const form = reactive({ username: "", password: "" });
const submitting = ref(false);
const error = ref<string | null>(null);

const canSubmit = computed(
  () => !submitting.value && form.username.length > 0 && form.password.length > 0,
);

async function submit(): Promise<void> {
  error.value = null;
  submitting.value = true;
  try {
    const state = await session.signIn(form.username, form.password);
    form.password = "";
    if (state === "authenticated") {
      const next = typeof route.query.next === "string" ? route.query.next : "/";
      await router.replace(next);
    }
    // state === "credentialChange" leaves the dialog to take over.
  } catch (caught) {
    // The server returns one message for unknown-user, wrong-password, locked,
    // and inactive. Echo it verbatim: paraphrasing risks reintroducing the
    // enumeration signal the server took care to remove (FR-A-11).
    error.value =
      caught instanceof ApiError
        ? caught.detail
        : caught instanceof NetworkError
          ? t("errors.network")
          : t("errors.unexpected");
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="login">
    <div class="panel">
      <div class="brand">
        <span class="mark" aria-hidden="true">◭</span>
        <h1>Cairn</h1>
        <p>{{ t("login.tagline") }}</p>
      </div>

      <el-alert
        v-if="error"
        :title="error"
        type="error"
        show-icon
        :closable="false"
        data-test="login-error"
        class="mb"
      />

      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item :label="t('login.username')">
          <el-input
            v-model="form.username"
            autocomplete="username"
            autofocus
            data-test="username"
          />
        </el-form-item>
        <el-form-item :label="t('login.password')">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
            data-test="password"
            @keyup.enter="canSubmit && submit()"
          />
        </el-form-item>
        <el-button
          type="primary"
          class="submit"
          :disabled="!canSubmit"
          :loading="submitting"
          data-test="submit"
          @click="submit"
        >
          {{ t("login.submit") }}
        </el-button>
      </el-form>

      <p class="footnote">{{ t("login.firstRunHint") }}</p>
    </div>

    <ForcedCredentialDialog />
  </div>
</template>

<style scoped>
.login {
  min-height: 100vh;
  display: grid;
  place-items: center;
  background: var(--cairn-surface-sunken);
  padding: var(--cairn-space-6);
}
.panel {
  width: 100%;
  max-width: 380px;
  background: var(--cairn-surface);
  border: 1px solid var(--cairn-border);
  border-radius: 10px;
  padding: var(--cairn-space-6);
  box-shadow: 0 1px 2px rgb(16 24 40 / 6%);
}
.brand {
  text-align: center;
  margin-bottom: var(--cairn-space-5);
}
.brand .mark {
  font-size: 30px;
  color: var(--cairn-accent);
  line-height: 1;
}
.brand h1 {
  margin: var(--cairn-space-2) 0 4px;
  font-size: 21px;
  letter-spacing: -0.01em;
}
.brand p {
  margin: 0;
  font-size: 13px;
  color: var(--cairn-text-muted);
}
.submit {
  width: 100%;
}
.mb {
  margin-bottom: var(--cairn-space-4);
}
.footnote {
  margin: var(--cairn-space-5) 0 0;
  font-size: 12px;
  color: var(--cairn-text-muted);
  text-align: center;
  line-height: 1.5;
}
</style>
