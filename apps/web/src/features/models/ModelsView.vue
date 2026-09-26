<script setup lang="ts">
/**
 * Embedding models: the provider endpoints this deployment can call and the
 * models registered on them. A model is tested against the live endpoint so
 * a wrong dimension surfaces here, not on the first upload.
 */
import { Blocks, KeyRound, MoreHorizontal, Plus, RefreshCw } from "lucide-vue-next";
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { ApiError } from "@/api/client";
import { knowledge } from "@/api/knowledge";
import { models as modelApi, providers as providerApi, type Model, type ModelTestResponse, type Provider } from "@/api/setup";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import Checkbox from "@/components/ui/Checkbox.vue";
import DropdownItem from "@/components/ui/DropdownItem.vue";
import DropdownMenu from "@/components/ui/DropdownMenu.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import NativeSelect from "@/components/ui/NativeSelect.vue";
import Notice from "@/components/ui/Notice.vue";
import Sheet from "@/components/ui/Sheet.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import PageHeader from "@/shared/components/PageHeader.vue";
import Section from "@/shared/components/Section.vue";
import { confirm } from "@/shared/composables/useConfirm";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError, isAbort } from "@/shared/errors";
import { healthTone } from "@/shared/pipeline";
import { FAMILIES, MODEL_PRESETS, type Family } from "@/features/models/families";

const { t } = useI18n();
const toasts = useToasts();

const providers = ref<Provider[]>([]);
const models = ref<Model[]>([]);
const tokenizers = ref<string[]>([]);
const loading = ref(true);
const error = ref<{ message: string; requestId?: string } | null>(null);
const controller = new AbortController();

const providerOpen = ref(false);
const modelOpen = ref(false);
const keyOpen = ref(false);
const submitting = ref(false);
const formError = ref<string | null>(null);
const fieldErrors = ref<Record<string, string[]>>({});
const testing = ref<string | null>(null);
const testResults = ref<Record<string, { ok: boolean; text: string }>>({});

const providerForm = reactive({
  name: "",
  family: "tei" as Family,
  base_url: FAMILIES.tei.baseUrl,
  api_key: "",
  allow_private: true,
  binding_revision: "minilm-1110a243",
  max_batch_size: FAMILIES.tei.batch,
});
const modelForm = reactive({
  provider_id: "",
  preset: "",
  model_key: "sentence-transformers/all-MiniLM-L6-v2",
  display_name: "MiniLM",
  dimension: "384" as string | number,
  max_input_tokens: 256,
  optimal_batch_size: 16,
  tokenizer_id: "minilm",
  normalize: true,
  query_prefix: "",
  sparse: false,
  send_dimension: false,
});
const keyForm = reactive({ provider: null as Provider | null, api_key: "" });

const providerName = computed(() => new Map(providers.value.map((provider) => [provider.id, provider.name])));
const providerFamily = computed(() => new Map(providers.value.map((provider) => [provider.id, provider.family as Family])));
const family = computed(() => FAMILIES[providerForm.family]);
const familyKeys = Object.keys(FAMILIES) as Family[];
const selectedFamily = computed<Family | undefined>(() => providerFamily.value.get(modelForm.provider_id));
const presets = computed(() => (selectedFamily.value ? MODEL_PRESETS[selectedFamily.value] ?? [] : []));
const familyCanSparse = computed(() => !!selectedFamily.value && FAMILIES[selectedFamily.value].sparse);
const familySendsDimension = computed(() => !!selectedFamily.value && FAMILIES[selectedFamily.value].sendsDimension);
const providerReady = computed(
  () =>
    !!providerForm.name.trim() &&
    !!providerForm.base_url.trim() &&
    (family.value.key !== "required" || !!providerForm.api_key.trim()),
);

watch(
  () => providerForm.family,
  (next) => {
    const spec = FAMILIES[next];
    providerForm.base_url = spec.baseUrl;
    providerForm.max_batch_size = spec.batch;
    providerForm.allow_private = spec.local;
    providerForm.binding_revision = next === "tei" ? "minilm-1110a243" : "v1";
    if (spec.key === "hidden") providerForm.api_key = "";
  },
);

watch(
  () => modelForm.provider_id,
  () => {
    const first = presets.value[0];
    if (first) applyPreset(first.id);
  },
);

function applyPreset(id: string): void {
  const preset = presets.value.find((candidate) => candidate.id === id);
  if (!preset) return;
  modelForm.preset = preset.id;
  modelForm.model_key = preset.model_key;
  modelForm.display_name = preset.display_name;
  modelForm.dimension = preset.dimension ?? "";
  modelForm.max_input_tokens = preset.max_input_tokens;
  modelForm.optimal_batch_size = preset.batch;
  modelForm.tokenizer_id = tokenizers.value.includes(preset.tokenizer_id) || !tokenizers.value.length ? preset.tokenizer_id : tokenizers.value[0]!;
  modelForm.normalize = true;
  modelForm.query_prefix = preset.query_prefix ?? "";
  modelForm.sparse = preset.sparse ?? false;
  modelForm.send_dimension = preset.send_dimension ?? false;
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const [providerRows, modelRows] = await Promise.all([
      providerApi.list(controller.signal),
      modelApi.list(controller.signal),
    ]);
    providers.value = providerRows;
    models.value = modelRows;
    try {
      tokenizers.value = (await knowledge.options(controller.signal)).tokenizers;
    } catch {
      tokenizers.value = [];
    }
  } catch (caught) {
    if (!isAbort(caught)) {
      const failure = describeError(caught);
      error.value = failure.requestId ? { message: failure.message, requestId: failure.requestId } : { message: failure.message };
    }
  } finally {
    loading.value = false;
  }
}

function openProvider(): void {
  formError.value = null;
  fieldErrors.value = {};
  providerForm.api_key = "";
  providerOpen.value = true;
}

function openModel(): void {
  formError.value = null;
  fieldErrors.value = {};
  if (!modelForm.provider_id && providers.value[0]) modelForm.provider_id = providers.value[0].id;
  if (!tokenizers.value.includes(modelForm.tokenizer_id) && tokenizers.value[0]) modelForm.tokenizer_id = tokenizers.value[0];
  modelOpen.value = true;
}

function openKey(provider: Provider): void {
  formError.value = null;
  keyForm.provider = provider;
  keyForm.api_key = "";
  keyOpen.value = true;
}

async function saveProvider(): Promise<void> {
  submitting.value = true;
  formError.value = null;
  fieldErrors.value = {};
  try {
    await providerApi.create({
      name: providerForm.name.trim(),
      family: providerForm.family,
      base_url: providerForm.base_url.trim(),
      api_key: providerForm.api_key.trim() || null,
      config: {
        allow_private: family.value.local && providerForm.allow_private,
        binding_revision: providerForm.binding_revision.trim() || "v1",
        max_batch_size: Number(providerForm.max_batch_size),
      },
    });
    providerOpen.value = false;
    providerForm.api_key = "";
    toasts.success(t("models.provider.created"));
    await load();
  } catch (caught) {
    formError.value = describeError(caught).message;
    if (caught instanceof ApiError) fieldErrors.value = caught.byField();
  } finally {
    submitting.value = false;
  }
}

async function saveKey(): Promise<void> {
  if (!keyForm.provider) return;
  submitting.value = true;
  formError.value = null;
  try {
    await providerApi.replaceKey(keyForm.provider.id, keyForm.api_key.trim() || null);
    keyOpen.value = false;
    keyForm.api_key = "";
    toasts.success(t("models.provider.keyReplaced"));
    await load();
  } catch (caught) {
    formError.value = describeError(caught).message;
  } finally {
    submitting.value = false;
  }
}

async function saveModel(): Promise<void> {
  submitting.value = true;
  formError.value = null;
  fieldErrors.value = {};
  try {
    const dimension = String(modelForm.dimension).trim();
    await modelApi.create({
      provider_id: modelForm.provider_id,
      model_key: modelForm.model_key.trim(),
      display_name: modelForm.display_name.trim(),
      capability: "embedding",
      dimension: dimension ? Number(dimension) : null,
      max_input_tokens: Number(modelForm.max_input_tokens),
      optimal_batch_size: Number(modelForm.optimal_batch_size),
      tokenizer_id: modelForm.tokenizer_id,
      normalize: modelForm.normalize,
      query_prefix: modelForm.query_prefix.trim() ? modelForm.query_prefix : null,
      sparse: familyCanSparse.value && modelForm.sparse,
      send_dimension: familySendsDimension.value && modelForm.send_dimension && !!dimension,
    });
    modelOpen.value = false;
    toasts.success(t("models.model.created"));
    await load();
  } catch (caught) {
    formError.value = describeError(caught).message;
    if (caught instanceof ApiError) fieldErrors.value = caught.byField();
  } finally {
    submitting.value = false;
  }
}

async function test(model: Model): Promise<void> {
  testing.value = model.id;
  try {
    const result: ModelTestResponse = await modelApi.test(model.id);
    const text = !result.healthy
      ? t("models.model.testFailed")
      : result.sparse_terms != null
        ? t("models.model.testOkSparse", { dims: result.dimensions, tokens: result.tokens, terms: result.sparse_terms })
        : t("models.model.testOk", { dims: result.dimensions, tokens: result.tokens });
    testResults.value = { ...testResults.value, [model.id]: { ok: result.healthy, text } };
    await load();
  } catch (caught) {
    testResults.value = { ...testResults.value, [model.id]: { ok: false, text: describeError(caught).message } };
  } finally {
    testing.value = null;
  }
}

async function removeProvider(provider: Provider): Promise<void> {
  const accepted = await confirm({
    title: t("models.provider.deleteTitle", { name: provider.name }),
    description: t("models.provider.deleteBody"),
    confirmLabel: t("common.delete"),
    danger: true,
  });
  if (!accepted) return;
  try {
    await providerApi.remove(provider.id);
    toasts.success(t("models.provider.deleted"));
    await load();
  } catch (caught) {
    toasts.error(t("common.deleteFailed"), describeError(caught).message);
  }
}

async function removeModel(model: Model): Promise<void> {
  const accepted = await confirm({
    title: t("models.model.deleteTitle", { name: model.display_name }),
    description: t("models.model.deleteBody"),
    confirmLabel: t("common.delete"),
    danger: true,
  });
  if (!accepted) return;
  try {
    await modelApi.remove(model.id);
    toasts.success(t("models.model.deleted"));
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
    <PageHeader :title="t('models.title')" :description="t('models.lede')">
      <template #actions>
        <Button variant="ghost" size="icon" :aria-label="t('common.refresh')" @click="load"><RefreshCw aria-hidden="true" /></Button>
        <Button variant="secondary" data-test="add-provider" @click="openProvider">
          <Plus aria-hidden="true" />
          {{ t("models.provider.add") }}
        </Button>
        <Button variant="primary" :disabled="!providers.length" data-test="add-model" @click="openModel">
          <Plus aria-hidden="true" />
          {{ t("models.model.add") }}
        </Button>
      </template>
    </PageHeader>

    <ErrorState v-if="error" :message="error.message" :request-id="error.requestId" class="mb-4" @retry="load" />

    <Section :title="t('models.provider.title')" :description="t('models.provider.description')" id="providers">
      <Skeleton v-if="loading" :rows="2" />
      <EmptyState v-else-if="!providers.length && !error" :title="t('models.provider.emptyTitle')" :description="t('models.provider.emptyBody')" compact>
        <template #icon><Blocks /></template>
        <Button variant="primary" size="sm" @click="openProvider">{{ t("models.provider.add") }}</Button>
      </EmptyState>
      <ul v-else class="divide-y divide-line rounded-md border border-line" data-test="provider-list">
        <li v-for="provider in providers" :key="provider.id" class="flex items-center gap-3 px-3 py-2.5">
          <div class="min-w-0 flex-1">
            <p class="flex flex-wrap items-center gap-2 text-[13.5px] font-medium text-ink">
              {{ provider.name }}
              <Badge size="sm" tone="neutral">{{ t(`models.families.${provider.family}.name`, provider.family) }}</Badge>
              <Badge v-if="provider.has_credentials" size="sm" tone="ok" data-test="provider-key"><KeyRound aria-hidden="true" class="size-3" />{{ t("models.provider.keySet") }}</Badge>
              <Badge v-if="!provider.is_enabled" size="sm" tone="warn">{{ t("common.disabled") }}</Badge>
            </p>
            <p class="truncate font-mono text-[12px] text-ink-3">{{ provider.base_url }}</p>
          </div>
          <span class="tnum text-[12.5px] text-ink-2">{{ t("models.provider.modelCount", { n: provider.model_count }) }}</span>
          <DropdownMenu>
            <template #trigger>
              <Button variant="ghost" size="icon-sm" :aria-label="t('common.actions')"><MoreHorizontal aria-hidden="true" /></Button>
            </template>
            <DropdownItem @select="modelForm.provider_id = provider.id; openModel()">{{ t("models.model.add") }}</DropdownItem>
            <DropdownItem data-test="replace-key" @select="openKey(provider)">{{ t("models.provider.replaceKey") }}</DropdownItem>
            <DropdownItem kind="separator" />
            <DropdownItem danger @select="removeProvider(provider)">{{ t("common.delete") }}</DropdownItem>
          </DropdownMenu>
        </li>
      </ul>
    </Section>

    <Section :title="t('models.model.title')" :description="t('models.model.description')" id="models">
      <Skeleton v-if="loading" :rows="2" />
      <EmptyState v-else-if="!models.length && !error" :title="t('models.model.emptyTitle')" :description="providers.length ? t('models.model.emptyBody') : t('models.model.emptyNeedsProvider')" compact>
        <Button v-if="providers.length" variant="primary" size="sm" @click="openModel">{{ t("models.model.add") }}</Button>
      </EmptyState>
      <ul v-else class="divide-y divide-line rounded-md border border-line" data-test="model-list">
        <li v-for="model in models" :key="model.id" class="grid gap-2 px-3 py-2.5 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-center">
          <div class="min-w-0">
            <p class="flex flex-wrap items-center gap-2 text-[13.5px] font-medium text-ink">
              {{ model.display_name }}
              <Badge size="sm" :tone="healthTone(model.health_state)" dot>{{ t(`models.health.${model.health_state}`, model.health_state) }}</Badge>
              <Badge v-if="model.sparse" size="sm" tone="brand" data-test="model-sparse">{{ t("models.model.sparseBadge") }}</Badge>
              <Badge v-if="!model.is_enabled" size="sm" tone="warn">{{ t("common.disabled") }}</Badge>
            </p>
            <p class="truncate font-mono text-[12px] text-ink-3">{{ model.model_key }}</p>
            <p class="tnum mt-0.5 text-[12px] text-ink-2">
              {{ providerName.get(model.provider_id) ?? model.provider_id }} · {{ model.dimension }}d · {{ t("models.model.tokenLimit", { n: model.max_input_tokens }) }} · {{ model.tokenizer_id }}
            </p>
            <p v-if="testResults[model.id]" :class="['mt-1 text-[12px]', testResults[model.id]!.ok ? 'text-ok' : 'text-bad']" data-test="model-test-result">
              {{ testResults[model.id]!.text }}
            </p>
          </div>
          <Button size="sm" variant="secondary" :loading="testing === model.id" data-test="test-model" @click="test(model)">{{ t("models.model.test") }}</Button>
          <DropdownMenu>
            <template #trigger>
              <Button variant="ghost" size="icon-sm" :aria-label="t('common.actions')"><MoreHorizontal aria-hidden="true" /></Button>
            </template>
            <DropdownItem danger @select="removeModel(model)">{{ t("common.delete") }}</DropdownItem>
          </DropdownMenu>
        </li>
      </ul>
    </Section>

    <Sheet v-model:open="providerOpen" :title="t('models.provider.add')" :description="t('models.provider.addHint')" test-id="provider-sheet">
      <form class="grid gap-3" autocomplete="off" @submit.prevent="saveProvider">
        <Notice v-if="formError" tone="bad">{{ formError }}</Notice>
        <Field :label="t('common.name')" required :error="fieldErrors.name" v-slot="{ id }">
          <Input :id="id" v-model="providerForm.name" data-test="provider-name" autofocus />
        </Field>
        <Field :label="t('models.provider.family')" :hint="t(`models.families.${providerForm.family}.hint`)" v-slot="{ id }">
          <NativeSelect :id="id" v-model="providerForm.family" data-test="provider-family">
            <option v-for="key in familyKeys" :key="key" :value="key">{{ t(`models.families.${key}.name`) }}</option>
          </NativeSelect>
        </Field>
        <Field :label="t('models.provider.endpoint')" required :hint="family.local ? t('models.provider.endpointHint') : t('models.provider.endpointHostedHint')" :error="fieldErrors.base_url" v-slot="{ id }">
          <Input :id="id" v-model="providerForm.base_url" data-test="provider-url" />
        </Field>
        <Field
          v-if="family.key !== 'hidden'"
          :label="t('models.provider.apiKey')"
          :required="family.key === 'required'"
          :hint="t('models.provider.apiKeyHint')"
          :error="fieldErrors.api_key"
          v-slot="{ id }"
        >
          <Input :id="id" v-model="providerForm.api_key" type="password" autocomplete="new-password" spellcheck="false" data-test="provider-key-input" />
        </Field>
        <div class="grid gap-3 sm:grid-cols-2">
          <Field :label="t('models.provider.revision')" :hint="t('models.provider.revisionHint')" v-slot="{ id }">
            <Input :id="id" v-model="providerForm.binding_revision" />
          </Field>
          <Field :label="t('models.provider.batch')" :hint="t('models.provider.batchHint')" v-slot="{ id }">
            <Input :id="id" v-model="providerForm.max_batch_size" type="number" min="1" max="1024" />
          </Field>
        </div>
        <Checkbox v-if="family.local" v-model="providerForm.allow_private">{{ t("models.provider.allowPrivate") }}</Checkbox>
        <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
      </form>
      <template #footer>
        <Button variant="ghost" @click="providerOpen = false">{{ t("common.cancel") }}</Button>
        <Button variant="primary" :loading="submitting" :disabled="!providerReady" data-test="provider-submit" @click="saveProvider">{{ t("common.save") }}</Button>
      </template>
    </Sheet>

    <Sheet v-model:open="keyOpen" :title="t('models.provider.replaceKey')" :description="t('models.provider.replaceKeyHint', { name: keyForm.provider?.name ?? '' })" test-id="key-sheet">
      <form class="grid gap-3" autocomplete="off" @submit.prevent="saveKey">
        <Notice v-if="formError" tone="bad">{{ formError }}</Notice>
        <Field :label="t('models.provider.apiKey')" :hint="t('models.provider.apiKeyHint')" v-slot="{ id }">
          <Input :id="id" v-model="keyForm.api_key" type="password" autocomplete="new-password" spellcheck="false" autofocus data-test="replace-key-input" />
        </Field>
        <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
      </form>
      <template #footer>
        <Button variant="ghost" @click="keyOpen = false">{{ t("common.cancel") }}</Button>
        <Button variant="primary" :loading="submitting" :disabled="!keyForm.api_key.trim()" data-test="replace-key-submit" @click="saveKey">{{ t("common.save") }}</Button>
      </template>
    </Sheet>

    <Sheet v-model:open="modelOpen" :title="t('models.model.add')" :description="t('models.model.addHint')" test-id="model-sheet">
      <form class="grid gap-3" @submit.prevent="saveModel">
        <Notice v-if="formError" tone="bad">{{ formError }}</Notice>
        <div class="grid gap-3 sm:grid-cols-2">
          <Field :label="t('models.provider.one')" required v-slot="{ id }">
            <NativeSelect :id="id" v-model="modelForm.provider_id" data-test="model-provider">
              <option v-for="provider in providers" :key="provider.id" :value="provider.id">{{ provider.name }}</option>
            </NativeSelect>
          </Field>
          <Field :label="t('models.model.preset')" :hint="t('models.model.presetHint')" v-slot="{ id }">
            <NativeSelect :id="id" :model-value="modelForm.preset" data-test="model-preset" :disabled="!presets.length" @update:model-value="applyPreset(String($event))">
              <option v-if="!presets.length" value="">{{ t("models.model.presetNone") }}</option>
              <option v-for="preset in presets" :key="preset.id" :value="preset.id">{{ preset.label }}</option>
            </NativeSelect>
          </Field>
        </div>
        <div class="grid gap-3 sm:grid-cols-2">
          <Field :label="t('models.model.displayName')" required :error="fieldErrors.display_name" v-slot="{ id }">
            <Input :id="id" v-model="modelForm.display_name" />
          </Field>
          <Field :label="t('models.model.key')" required :hint="selectedFamily === 'volcengine' ? t('models.model.keyHintArk') : undefined" :error="fieldErrors.model_key" v-slot="{ id }">
            <Input :id="id" v-model="modelForm.model_key" data-test="model-key" />
          </Field>
          <Field :label="t('models.model.dimension')" :hint="t('models.model.dimensionHint')" :error="fieldErrors.dimension" v-slot="{ id }">
            <Input :id="id" v-model="modelForm.dimension" type="number" min="1" max="65536" :placeholder="t('models.model.dimensionAuto')" data-test="model-dimension" />
          </Field>
          <Field :label="t('models.model.maxTokens')" required :error="fieldErrors.max_input_tokens" v-slot="{ id }">
            <Input :id="id" v-model="modelForm.max_input_tokens" type="number" min="1" max="32768" />
          </Field>
          <Field :label="t('models.model.tokenizer')" required :hint="t('models.model.tokenizerHint')" v-slot="{ id }">
            <NativeSelect v-if="tokenizers.length" :id="id" v-model="modelForm.tokenizer_id">
              <option v-for="tokenizer in tokenizers" :key="tokenizer" :value="tokenizer">{{ tokenizer }}</option>
            </NativeSelect>
            <Input v-else :id="id" v-model="modelForm.tokenizer_id" />
          </Field>
          <Field :label="t('models.model.batchSize')" v-slot="{ id }">
            <Input :id="id" v-model="modelForm.optimal_batch_size" type="number" min="1" max="1024" />
          </Field>
        </div>
        <Field :label="t('models.model.queryPrefix')" :hint="t('models.model.queryPrefixHint')" v-slot="{ id }">
          <Input :id="id" v-model="modelForm.query_prefix" maxlength="1000" />
        </Field>
        <Checkbox v-model="modelForm.normalize">{{ t("models.model.normalize") }}</Checkbox>
        <Checkbox v-if="familyCanSparse" v-model="modelForm.sparse" data-test="model-sparse-toggle">{{ t("models.model.sparse") }}</Checkbox>
        <Checkbox v-if="familySendsDimension" v-model="modelForm.send_dimension">{{ t("models.model.sendDimension") }}</Checkbox>
        <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
      </form>
      <template #footer>
        <Button variant="ghost" @click="modelOpen = false">{{ t("common.cancel") }}</Button>
        <Button variant="primary" :loading="submitting" :disabled="!modelForm.provider_id || !modelForm.tokenizer_id || !modelForm.model_key.trim()" data-test="model-submit" @click="saveModel">{{ t("models.model.register") }}</Button>
      </template>
    </Sheet>
  </div>
</template>
