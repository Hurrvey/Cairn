<script setup lang="ts">
import { LockKeyhole, UserRound } from "lucide-vue-next";
import { computed, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";

import { ApiError, NetworkError } from "@/api/client";
import BrandMark from "@/components/ui/BrandMark.vue";
import Button from "@/components/ui/Button.vue";
import ContourField from "@/components/ui/ContourField.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import Notice from "@/components/ui/Notice.vue";
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

async function enterApplication(): Promise<void> {
  const next = typeof route.query.next === "string" ? route.query.next : "/";
  await router.replace(next);
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return;
  error.value = null;
  submitting.value = true;
  try {
    const state = await session.signIn(form.username, form.password);
    form.password = "";
    if (state === "authenticated") {
      await enterApplication();
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
  <div class="relative flex min-h-screen items-center justify-center overflow-hidden bg-bg px-4 py-10">
    <ContourField :opacity="0.5" />
    <div class="relative grid w-full max-w-[880px] items-center gap-10 md:grid-cols-[1.1fr_1fr]">
      <section class="hidden md:block">
        <BrandMark :size="44" />
        <h1 class="mt-6 max-w-[14ch] text-[40px] font-semibold leading-[1.05] text-ink">{{ t("login.headline") }}</h1>
        <p class="mt-4 max-w-[38ch] text-[15px] leading-relaxed text-ink-2">{{ t("login.tagline") }}</p>
      </section>

      <section class="w-full rounded-xl border border-line bg-surface/95 p-6 shadow-pop backdrop-blur sm:p-7" data-test="login-panel">
        <div class="mb-5 flex items-center gap-2 md:hidden">
          <BrandMark :size="24" wordmark />
        </div>
        <h2 class="text-[19px] font-semibold text-ink">{{ t("login.title") }}</h2>
        <p class="mt-1 text-[13px] text-ink-2">{{ t("login.subtitle") }}</p>

        <form class="mt-5 grid gap-4" @submit.prevent="submit">
          <Notice v-if="error" tone="bad" test-id="login-error">{{ error }}</Notice>
          <Field :label="t('login.username')" v-slot="{ id }">
            <Input :id="id" v-model="form.username" autocomplete="username" autofocus data-test="username">
              <template #prefix><UserRound /></template>
            </Input>
          </Field>
          <Field :label="t('login.password')" v-slot="{ id }">
            <Input :id="id" v-model="form.password" type="password" autocomplete="current-password" data-test="password">
              <template #prefix><LockKeyhole /></template>
            </Input>
          </Field>
          <Button type="submit" variant="primary" size="lg" block :disabled="!canSubmit" :loading="submitting" data-test="submit">
            {{ t("login.submit") }}
          </Button>
        </form>

        <p class="mt-5 text-[12px] leading-relaxed text-ink-3">{{ t("login.firstRunHint") }}</p>
      </section>
    </div>

    <ForcedCredentialDialog @completed="enterApplication" />
  </div>
</template>
