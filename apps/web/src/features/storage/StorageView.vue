<script setup lang="ts">
/**
 * Storage bindings: where documents are kept and where vectors are indexed.
 * Local objects and Qdrant are the drivers this build ships; the page
 * says so rather than offering options that would fail.
 */
import { Database, MoreHorizontal, Plus, RefreshCw } from "lucide-vue-next";
import { onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";

import { ApiError } from "@/api/client";
import { bindings as bindingApi, type Binding } from "@/api/setup";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import DropdownItem from "@/components/ui/DropdownItem.vue";
import DropdownMenu from "@/components/ui/DropdownMenu.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import Notice from "@/components/ui/Notice.vue";
import RadioCards from "@/components/ui/RadioCards.vue";
import NativeSelect from "@/components/ui/NativeSelect.vue";
import Switch from "@/components/ui/Switch.vue";
import Sheet from "@/components/ui/Sheet.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import PageHeader from "@/shared/components/PageHeader.vue";
import { confirm } from "@/shared/composables/useConfirm";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError, isAbort } from "@/shared/errors";
import { healthTone } from "@/shared/pipeline";

const { t } = useI18n();
const toasts = useToasts();

const rows = ref<Binding[]>([]);
const loading = ref(true);
const error = ref<{ message: string; requestId?: string } | null>(null);
const controller = new AbortController();
const sheetOpen = ref(false);
const submitting = ref(false);
const formError = ref<string | null>(null);
const fieldErrors = ref<Record<string, string[]>>({});
const testing = ref<string | null>(null);
const testResults = ref<Record<string, { ok: boolean; text: string }>>({});
const form = reactive({ name: "", kind: "object" as "object" | "vector" });
const s3 = reactive({ driver: "local", endpoint: "", bucket: "", region: "us-east-1", prefix: "", allow_private: false, access_key_id: "", secret_access_key: "" });
const rotating = ref<Binding | null>(null);

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    rows.value = await bindingApi.list(controller.signal);
  } catch (caught) {
    if (!isAbort(caught)) {
      const failure = describeError(caught);
      error.value = failure.requestId ? { message: failure.message, requestId: failure.requestId } : { message: failure.message };
    }
  } finally {
    loading.value = false;
  }
}

function openSheet(): void {
  rotating.value = null;
  Object.assign(s3, { driver: "local", endpoint: "", bucket: "", region: "us-east-1", prefix: "", allow_private: false, access_key_id: "", secret_access_key: "" });
  form.name = "";
  form.kind = rows.value.some((row) => row.kind === "object") && !rows.value.some((row) => row.kind === "vector") ? "vector" : "object";
  formError.value = null;
  fieldErrors.value = {};
  sheetOpen.value = true;
}

async function save(): Promise<void> {
  submitting.value = true;
  formError.value = null;
  fieldErrors.value = {};
  try {
    const remote = form.kind === "object" && s3.driver === "s3";
    const credentials = { access_key_id: s3.access_key_id, secret_access_key: s3.secret_access_key };
    const created = rotating.value ? await bindingApi.replaceCredentials(rotating.value.id, credentials) : await bindingApi.create({
      name: form.name.trim(),
      kind: form.kind,
      driver: form.kind === "object" ? s3.driver : "qdrant",
      config: remote ? { bucket: s3.bucket, endpoint: s3.endpoint || null, region: s3.region, prefix: s3.prefix, path_style: true, allow_private: s3.allow_private } : {},
      ...(remote && s3.access_key_id ? { credentials } : {}),
      is_default: false,
    });
    sheetOpen.value = false;
    s3.secret_access_key = "";
    toasts.success(t("storage.created", { name: created.name }));
    await load();
    await test(created);
  } catch (caught) {
    formError.value = describeError(caught).message;
    if (caught instanceof ApiError) fieldErrors.value = caught.byField();
  } finally {
    submitting.value = false;
  }
}

function rotate(binding: Binding): void {
  openSheet();
  rotating.value = binding;
  form.kind = "object";
  form.name = binding.name;
  s3.driver = "s3";
}

async function test(binding: Binding): Promise<void> {
  testing.value = binding.id;
  try {
    const result = await bindingApi.test(binding.id);
    testResults.value = {
      ...testResults.value,
      [binding.id]: { ok: result.healthy, text: result.healthy ? t("storage.testOk") : t("storage.testFailed") },
    };
    await load();
  } catch (caught) {
    testResults.value = { ...testResults.value, [binding.id]: { ok: false, text: describeError(caught).message } };
  } finally {
    testing.value = null;
  }
}

async function remove(binding: Binding): Promise<void> {
  const accepted = await confirm({
    title: t("storage.deleteTitle", { name: binding.name }),
    description: t("storage.deleteBody"),
    confirmLabel: t("common.delete"),
    danger: true,
  });
  if (!accepted) return;
  try {
    await bindingApi.remove(binding.id);
    toasts.success(t("storage.deleted"));
    await load();
  } catch (caught) {
    toasts.error(t("common.deleteFailed"), describeError(caught).message);
  }
}

onMounted(load);
onBeforeUnmount(() => controller.abort());
</script>

<template>
  <div>
    <PageHeader :title="t('storage.title')" :description="t('storage.lede')">
      <template #actions>
        <Button variant="ghost" size="icon" :aria-label="t('common.refresh')" @click="load"><RefreshCw aria-hidden="true" /></Button>
        <Button variant="primary" data-test="add-storage" @click="openSheet">
          <Plus aria-hidden="true" />
          {{ t("storage.add") }}
        </Button>
      </template>
    </PageHeader>

    <ErrorState v-if="error" :message="error.message" :request-id="error.requestId" class="mb-4" @retry="load" />

    <Skeleton v-if="loading" :rows="2" />
    <EmptyState v-else-if="!rows.length && !error" :title="t('storage.emptyTitle')" :description="t('storage.emptyBody')">
      <template #icon><Database /></template>
      <Button variant="primary" size="sm" @click="openSheet">{{ t("storage.add") }}</Button>
    </EmptyState>
    <ul v-else class="divide-y divide-line rounded-md border border-line" data-test="binding-list">
      <li v-for="binding in rows" :key="binding.id" class="grid gap-2 px-3 py-2.5 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-center" :data-test="`binding-${binding.id}`">
        <div class="min-w-0">
          <p class="flex flex-wrap items-center gap-2 text-[13.5px] font-medium text-ink">
            {{ binding.name }}
            <Badge size="sm" tone="brand">{{ t(`storage.kinds.${binding.kind}`) }}</Badge>
            <Badge size="sm" tone="neutral" mono>{{ binding.driver }}</Badge>
            <Badge v-if="binding.has_credentials" size="sm" tone="neutral">{{ t("storage.hasCredentials") }}</Badge>
            <Badge size="sm" :tone="healthTone(binding.health_state)" dot data-test="binding-health">{{ t(`storage.health.${binding.health_state}`, binding.health_state) }}</Badge>
          </p>
          <p class="text-[12px] text-ink-3">{{ binding.kind === "object" ? (binding.driver === "local" ? t("storage.localHint") : t("storage.s3Hint")) : t("storage.qdrantHint") }}</p>
          <p v-if="testResults[binding.id]" :class="['mt-1 text-[12px]', testResults[binding.id]!.ok ? 'text-ok' : 'text-bad']" data-test="binding-test-result">
            {{ testResults[binding.id]!.text }}
          </p>
        </div>
        <Button size="sm" variant="secondary" :loading="testing === binding.id" data-test="test-binding" @click="test(binding)">{{ t("storage.test") }}</Button>
        <DropdownMenu>
          <template #trigger>
            <Button variant="ghost" size="icon-sm" :aria-label="t('common.actions')"><MoreHorizontal aria-hidden="true" /></Button>
          </template>
          <DropdownItem danger @select="remove(binding)">{{ t("common.delete") }}</DropdownItem>
          <DropdownItem v-if="binding.kind === 'object' && binding.driver !== 'local'" @select="rotate(binding)">{{ t("storage.rotateCredentials") }}</DropdownItem>
        </DropdownMenu>
      </li>
    </ul>

    <Sheet v-model:open="sheetOpen" :title="t(rotating ? 'storage.rotateCredentials' : 'storage.add')" :description="t('storage.addHint')" test-id="storage-sheet">
      <form class="grid gap-4" @submit.prevent="save">
        <Notice v-if="formError" tone="bad">{{ formError }}</Notice>
        <Field :label="t('common.name')" required :error="fieldErrors.name" v-slot="{ id }">
          <Input :id="id" v-model="form.name" data-test="storage-name" autofocus />
        </Field>
        <Field v-if="!rotating" :label="t('storage.kind')">
          <RadioCards
            v-model="form.kind"
            :options="[
              { value: 'object', label: t('storage.kinds.object'), description: t('storage.localHint') },
              { value: 'vector', label: t('storage.kinds.vector'), description: t('storage.qdrantHint') },
            ]"
            test-id="storage-kind"
          />
        </Field>
        <template v-if="form.kind === 'object'">
          <Field v-if="!rotating" :label="t('storage.driver')" v-slot="{ id }">
            <NativeSelect :id="id" v-model="s3.driver" data-test="storage-driver"><option value="local">{{ t('storage.localDriver') }}</option><option value="s3">{{ t('storage.s3Driver') }}</option></NativeSelect>
          </Field>
          <template v-if="s3.driver === 's3'">
            <template v-if="!rotating">
              <Field :label="t('storage.endpoint')" v-slot="{ id }"><Input :id="id" v-model="s3.endpoint" placeholder="https://s3.example.com" /></Field>
              <Field :label="t('storage.bucket')" required v-slot="{ id }"><Input :id="id" v-model="s3.bucket" data-test="storage-bucket" /></Field>
              <Field :label="t('storage.region')" v-slot="{ id }"><Input :id="id" v-model="s3.region" /></Field>
              <Field :label="t('storage.prefix')" v-slot="{ id }"><Input :id="id" v-model="s3.prefix" /></Field>
              <Switch v-model="s3.allow_private" :label="t('storage.allowPrivate')" />
            </template>
            <Field :label="t('storage.accessKey')" v-slot="{ id }"><Input :id="id" v-model="s3.access_key_id" autocomplete="off" data-test="storage-access-key" /></Field>
            <Field :label="t('storage.secretKey')" v-slot="{ id }"><Input :id="id" v-model="s3.secret_access_key" type="password" autocomplete="new-password" data-test="storage-secret-key" /></Field>
          </template>
        </template>
        <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
      </form>
      <template #footer>
        <Button variant="ghost" @click="sheetOpen = false">{{ t("common.cancel") }}</Button>
        <Button variant="primary" :loading="submitting" :disabled="!form.name.trim()" data-test="storage-submit" @click="save">{{ t(rotating ? "storage.rotateCredentials" : "storage.createAndTest") }}</Button>
      </template>
    </Sheet>
  </div>
</template>
