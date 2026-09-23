<script setup lang="ts">
/**
 * Create a knowledge base. The model and both storage bindings are chosen
 * here because they are frozen into the first index version; changing the
 * model later means a rebuild, and the sheet says so.
 */
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { ApiError } from "@/api/client";
import { knowledge, type KnowledgeBase, type SetupOptions } from "@/api/knowledge";
import Button from "@/components/ui/Button.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import NativeSelect from "@/components/ui/NativeSelect.vue";
import Notice from "@/components/ui/Notice.vue";
import Sheet from "@/components/ui/Sheet.vue";
import Textarea from "@/components/ui/Textarea.vue";
import { describeError } from "@/shared/errors";

const open = defineModel<boolean>("open", { default: false });
const emit = defineEmits<{ created: [kb: KnowledgeBase] }>();
const { t } = useI18n();

const options = ref<SetupOptions | null>(null);
const loadingOptions = ref(false);
const submitting = ref(false);
const error = ref<string | null>(null);
const fieldErrors = ref<Record<string, string[]>>({});

const form = reactive({
  name: "",
  description: "",
  embedding_model_id: "",
  object_binding_id: "",
  vector_binding_id: "",
  search_mode: "hybrid" as "hybrid" | "vector" | "fulltext",
  strategy: "parent_child" as "parent_child" | "recursive" | "markdown" | "fixed" | "semantic",
});

const objectBindings = computed(() => options.value?.bindings.filter((binding) => binding.kind === "object") ?? []);
const vectorBindings = computed(() => options.value?.bindings.filter((binding) => binding.kind === "vector") ?? []);
const setupMissing = computed(
  () => !!options.value && (!options.value.models.length || !objectBindings.value.length || !vectorBindings.value.length),
);
const canSubmit = computed(
  () =>
    !submitting.value &&
    form.name.trim().length > 0 &&
    !!form.embedding_model_id &&
    !!form.object_binding_id &&
    !!form.vector_binding_id,
);

watch(open, async (value) => {
  if (!value) return;
  Object.assign(form, {
    name: "",
    description: "",
    embedding_model_id: "",
    object_binding_id: "",
    vector_binding_id: "",
    search_mode: "hybrid",
    strategy: "parent_child",
  });
  error.value = null;
  fieldErrors.value = {};
  loadingOptions.value = true;
  try {
    options.value = await knowledge.options();
    // Preselect when there is exactly one sensible choice; the common case
    // for a fresh deployment with the local model.
    if (options.value.models.length === 1) form.embedding_model_id = options.value.models[0]!.id;
    if (objectBindings.value.length === 1) form.object_binding_id = objectBindings.value[0]!.id;
    if (vectorBindings.value.length === 1) form.vector_binding_id = vectorBindings.value[0]!.id;
  } catch (caught) {
    error.value = describeError(caught).message;
  } finally {
    loadingOptions.value = false;
  }
});

async function submit(): Promise<void> {
  if (!canSubmit.value) return;
  submitting.value = true;
  error.value = null;
  fieldErrors.value = {};
  try {
    const created = await knowledge.create({
      name: form.name.trim(),
      description: form.description.trim() || null,
      embedding_model_id: form.embedding_model_id,
      object_binding_id: form.object_binding_id,
      vector_binding_id: form.vector_binding_id,
      metric: "cosine",
      chunk_config: {
        strategy: form.strategy,
        child_tokens: 512,
        child_overlap: 64,
        parent_tokens: 2048,
        keep_tables_intact: true,
        min_chunk_tokens: 32,
        prepend_heading_path: true,
      },
      retrieval_config: {
        search_mode: form.search_mode,
        top_k: 5,
        candidate_k: 100,
        score_threshold: 0,
        dedupe: "none",
        rerank: { enabled: false, top_n: 5, timeout_s: 1.5 },
        expand_parent: false,
      },
    });
    emit("created", created);
  } catch (caught) {
    error.value = describeError(caught).message;
    if (caught instanceof ApiError) fieldErrors.value = caught.byField();
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <Sheet v-model:open="open" :title="t('knowledge.create.title')" :description="t('knowledge.create.description')" test-id="create-kb-sheet">
    <form class="grid gap-4" @submit.prevent="submit">
      <Notice v-if="error" tone="bad" test-id="form-error">{{ error }}</Notice>
      <Notice v-if="setupMissing" tone="warn">
        {{ t("knowledge.create.setupMissing") }}
        <RouterLink :to="{ name: 'models' }" class="ml-1 text-brand underline-offset-2 hover:underline">{{ t("nav.models") }}</RouterLink>
        ·
        <RouterLink :to="{ name: 'storage' }" class="text-brand underline-offset-2 hover:underline">{{ t("nav.storage") }}</RouterLink>
      </Notice>

      <Field :label="t('common.name')" required :error="fieldErrors.name" v-slot="{ id }">
        <Input :id="id" v-model="form.name" maxlength="255" data-test="kb-name" autofocus />
      </Field>
      <Field :label="t('common.description')" :error="fieldErrors.description" v-slot="{ id }">
        <Textarea :id="id" v-model="form.description" maxlength="4000" rows="3" />
      </Field>

      <Field :label="t('knowledge.create.model')" required :hint="t('knowledge.create.modelHint')" v-slot="{ id }">
        <NativeSelect :id="id" v-model="form.embedding_model_id" data-test="kb-model" :disabled="loadingOptions">
          <option value="" disabled>{{ t("common.select") }}</option>
          <option v-for="model in options?.models ?? []" :key="model.id" :value="model.id">
            {{ model.display_name }} · {{ model.dimension }}d
          </option>
        </NativeSelect>
      </Field>

      <div class="grid gap-4 sm:grid-cols-2">
        <Field :label="t('knowledge.create.objectStorage')" required v-slot="{ id }">
          <NativeSelect :id="id" v-model="form.object_binding_id" data-test="kb-objects" :disabled="loadingOptions">
            <option value="" disabled>{{ t("common.select") }}</option>
            <option v-for="binding in objectBindings" :key="binding.id" :value="binding.id">{{ binding.name }}</option>
          </NativeSelect>
        </Field>
        <Field :label="t('knowledge.create.vectorStorage')" required v-slot="{ id }">
          <NativeSelect :id="id" v-model="form.vector_binding_id" data-test="kb-vectors" :disabled="loadingOptions">
            <option value="" disabled>{{ t("common.select") }}</option>
            <option v-for="binding in vectorBindings" :key="binding.id" :value="binding.id">{{ binding.name }}</option>
          </NativeSelect>
        </Field>
      </div>

      <div class="grid gap-4 sm:grid-cols-2">
        <Field :label="t('knowledge.settings.chunking.strategy')" :hint="t('knowledge.create.strategyHint')" v-slot="{ id }">
          <NativeSelect :id="id" v-model="form.strategy">
            <option v-for="strategy in ['parent_child', 'recursive', 'markdown', 'fixed', 'semantic']" :key="strategy" :value="strategy">
              {{ t(`knowledge.settings.chunking.strategies.${strategy}`) }}
            </option>
          </NativeSelect>
        </Field>
        <Field :label="t('knowledge.settings.retrieval.searchMode')" v-slot="{ id }">
          <NativeSelect :id="id" v-model="form.search_mode">
            <option value="hybrid">{{ t("knowledge.search.modes.hybrid") }}</option>
            <option value="vector">{{ t("knowledge.search.modes.vector") }}</option>
            <option value="fulltext">{{ t("knowledge.search.modes.fulltext") }}</option>
          </NativeSelect>
        </Field>
      </div>
      <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
    </form>
    <template #footer>
      <Button variant="ghost" @click="open = false">{{ t("common.cancel") }}</Button>
      <Button variant="primary" :disabled="!canSubmit" :loading="submitting" data-test="kb-submit" @click="submit">{{ t("common.create") }}</Button>
    </template>
  </Sheet>
</template>
