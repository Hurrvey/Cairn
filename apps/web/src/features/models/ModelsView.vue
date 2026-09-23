<script setup lang="ts">
/**
 * Embedding models: the provider endpoints this deployment can call and the
 * models registered on them. A model is tested against the live endpoint so
 * a wrong dimension surfaces here, not on the first upload.
 */
import { Blocks, MoreHorizontal, Plus, RefreshCw } from "lucide-vue-next";
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
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
const submitting = ref(false);
const formError = ref<string | null>(null);
const fieldErrors = ref<Record<string, string[]>>({});
const testing = ref<string | null>(null);
const testResults = ref<Record<string, { ok: boolean; text: string }>>({});

const providerForm = reactive({
  name: "",
  family: "tei" as "tei" | "infinity",
  base_url: "http://embedding:80",
  allow_private: true,
  binding_revision: "minilm-1110a243",
});
const modelForm = reactive({
  provider_id: "",
  model_key: "sentence-transformers/all-MiniLM-L6-v2",
  display_name: "MiniLM",
  dimension: 384,
  max_input_tokens: 256,
  optimal_batch_size: 16,
  tokenizer_id: "minilm",
  normalize: true,
});

const providerName = computed(() => new Map(providers.value.map((provider) => [provider.id, provider.name])));

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
  providerOpen.value = true;
}

function openModel(): void {
  formError.value = null;
  fieldErrors.value = {};
  if (!modelForm.provider_id && providers.value[0]) modelForm.provider_id = providers.value[0].id;
  if (!tokenizers.value.includes(modelForm.tokenizer_id) && tokenizers.value[0]) modelForm.tokenizer_id = tokenizers.value[0];
  modelOpen.value = true;
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
      config: {
        allow_private: providerForm.allow_private,
        binding_revision: providerForm.binding_revision.trim(),
        max_batch_size: 16,
      },
    });
    providerOpen.value = false;
    toasts.success(t("models.provider.created"));
    await load();
  } catch (caught) {
    formError.value = describeError(caught).message;
    if (caught instanceof ApiError) fieldErrors.value = caught.byField();
  } finally {
    submitting.value = false;
  }
}

async function saveModel(): Promise<void> {
  submitting.value = true;
  formError.value = null;
  fieldErrors.value = {};
  try {
    await modelApi.create({
      provider_id: modelForm.provider_id,
      model_key: modelForm.model_key.trim(),
      display_name: modelForm.display_name.trim(),
      capability: "embedding",
      dimension: Number(modelForm.dimension),
      max_input_tokens: Number(modelForm.max_input_tokens),
      optimal_batch_size: Number(modelForm.optimal_batch_size),
      tokenizer_id: modelForm.tokenizer_id,
      normalize: modelForm.normalize,
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
    testResults.value = {
      ...testResults.value,
      [model.id]: {
        ok: result.healthy,
        text: result.healthy
          ? t("models.model.testOk", { dims: result.dimensions, tokens: result.tokens })
          : t("models.model.testFailed"),
      },
    };
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
            <p class="flex items-center gap-2 text-[13.5px] font-medium text-ink">
              {{ provider.name }}
              <Badge size="sm" tone="neutral" mono>{{ provider.family }}</Badge>
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
      <form class="grid gap-3" @submit.prevent="saveProvider">
        <Notice v-if="formError" tone="bad">{{ formError }}</Notice>
        <Field :label="t('common.name')" required :error="fieldErrors.name" v-slot="{ id }">
          <Input :id="id" v-model="providerForm.name" data-test="provider-name" autofocus />
        </Field>
        <Field :label="t('models.provider.dialect')" v-slot="{ id }">
          <NativeSelect :id="id" v-model="providerForm.family">
            <option value="tei">TEI</option>
            <option value="infinity">Infinity</option>
          </NativeSelect>
        </Field>
        <Field :label="t('models.provider.endpoint')" required :hint="t('models.provider.endpointHint')" :error="fieldErrors.base_url" v-slot="{ id }">
          <Input :id="id" v-model="providerForm.base_url" data-test="provider-url" />
        </Field>
        <Field :label="t('models.provider.revision')" :hint="t('models.provider.revisionHint')" v-slot="{ id }">
          <Input :id="id" v-model="providerForm.binding_revision" />
        </Field>
        <Checkbox v-model="providerForm.allow_private">{{ t("models.provider.allowPrivate") }}</Checkbox>
        <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
      </form>
      <template #footer>
        <Button variant="ghost" @click="providerOpen = false">{{ t("common.cancel") }}</Button>
        <Button variant="primary" :loading="submitting" :disabled="!providerForm.name.trim() || !providerForm.base_url.trim()" data-test="provider-submit" @click="saveProvider">{{ t("common.save") }}</Button>
      </template>
    </Sheet>

    <Sheet v-model:open="modelOpen" :title="t('models.model.add')" :description="t('models.model.addHint')" test-id="model-sheet">
      <form class="grid gap-3" @submit.prevent="saveModel">
        <Notice v-if="formError" tone="bad">{{ formError }}</Notice>
        <Field :label="t('models.provider.one')" required v-slot="{ id }">
          <NativeSelect :id="id" v-model="modelForm.provider_id" data-test="model-provider">
            <option v-for="provider in providers" :key="provider.id" :value="provider.id">{{ provider.name }}</option>
          </NativeSelect>
        </Field>
        <div class="grid gap-3 sm:grid-cols-2">
          <Field :label="t('models.model.displayName')" required :error="fieldErrors.display_name" v-slot="{ id }">
            <Input :id="id" v-model="modelForm.display_name" />
          </Field>
          <Field :label="t('models.model.key')" required :error="fieldErrors.model_key" v-slot="{ id }">
            <Input :id="id" v-model="modelForm.model_key" />
          </Field>
          <Field :label="t('models.model.dimension')" required :error="fieldErrors.dimension" v-slot="{ id }">
            <Input :id="id" v-model="modelForm.dimension" type="number" min="1" max="65536" />
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
        <Checkbox v-model="modelForm.normalize">{{ t("models.model.normalize") }}</Checkbox>
        <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
      </form>
      <template #footer>
        <Button variant="ghost" @click="modelOpen = false">{{ t("common.cancel") }}</Button>
        <Button variant="primary" :loading="submitting" :disabled="!modelForm.provider_id || !modelForm.tokenizer_id || !modelForm.model_key.trim()" data-test="model-submit" @click="saveModel">{{ t("models.model.register") }}</Button>
      </template>
    </Sheet>
  </div>
</template>
