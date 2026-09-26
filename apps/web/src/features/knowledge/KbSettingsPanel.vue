<script setup lang="ts">
/**
 * Knowledge base settings. Retrieval defaults apply immediately to every
 * query that does not override them; chunking changes only take effect on
 * the next rebuild, and the form says so rather than implying otherwise.
 */
import { computed, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";

import { ApiError } from "@/api/client";
import { knowledge, type KnowledgeBase, type SetupOptions } from "@/api/knowledge";
import Button from "@/components/ui/Button.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import NativeSelect from "@/components/ui/NativeSelect.vue";
import Notice from "@/components/ui/Notice.vue";
import Switch from "@/components/ui/Switch.vue";
import Textarea from "@/components/ui/Textarea.vue";
import Section from "@/shared/components/Section.vue";
import { autoSparseModel, parseSparseValue, type SparseChoice } from "@/features/knowledge/sparse";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError } from "@/shared/errors";

const props = defineProps<{ kb: KnowledgeBase; options: SetupOptions | undefined; building?: boolean }>();
const emit = defineEmits<{ saved: []; delete: []; rebuild: [sparse: SparseChoice] }>();
const { t } = useI18n();
const toasts = useToasts();

const general = reactive({ name: props.kb.name, description: props.kb.description ?? "" });
const retrieval = reactive({
  search_mode: props.kb.retrieval_config.search_mode ?? "hybrid",
  top_k: props.kb.retrieval_config.top_k ?? 5,
  candidate_k: props.kb.retrieval_config.candidate_k ?? 100,
  score_threshold: props.kb.retrieval_config.score_threshold ?? 0,
  expand_parent: props.kb.retrieval_config.expand_parent ?? false,
  fusion: props.kb.retrieval_config.fusion?.method ?? "rrf",
  dense: props.kb.retrieval_config.weights?.dense ?? 0.7,
  sparse: props.kb.retrieval_config.weights?.sparse ?? 0.3,
  rerank_enabled: props.kb.retrieval_config.rerank?.enabled ?? false,
  rerank_model: props.kb.retrieval_config.rerank?.model_id ?? "",
  rerank_top_n: props.kb.retrieval_config.rerank?.top_n ?? 5,
});
const chunking = reactive({
  strategy: props.kb.chunk_config.strategy ?? "parent_child",
  child_tokens: props.kb.chunk_config.child_tokens ?? 512,
  child_overlap: props.kb.chunk_config.child_overlap ?? 64,
  parent_tokens: props.kb.chunk_config.parent_tokens ?? 2048,
  min_chunk_tokens: props.kb.chunk_config.min_chunk_tokens ?? 32,
  keep_tables_intact: props.kb.chunk_config.keep_tables_intact ?? true,
  prepend_heading_path: props.kb.chunk_config.prepend_heading_path ?? true,
});

const saving = ref<"general" | "retrieval" | "chunking" | null>(null);
const errors = reactive<{ general: string | null; retrieval: string | null; chunking: string | null }>({ general: null, retrieval: null, chunking: null });
const fieldErrors = ref<Record<string, string[]>>({});

const keywordNext = ref(props.kb.sparse_kind === "model" && props.kb.sparse_model_id ? `model:${props.kb.sparse_model_id}` : "bm25");
const sparseModels = computed(() => props.options?.models.filter((model) => model.sparse) ?? []);
function sparseName(kind: string, modelId: string | null | undefined): string {
  if (kind !== "model") return t("knowledge.create.keywordBm25");
  const model = props.options?.models.find((item) => item.id === modelId);
  return t("knowledge.create.keywordModel", { name: model?.display_name ?? modelId ?? "" });
}
const keywordCurrent = computed(() => sparseName(props.kb.sparse_kind ?? "bm25", props.kb.sparse_model_id));
const keywordAuto = computed(() => {
  const model = autoSparseModel(props.kb.embedding_model_id, props.options?.models ?? []);
  return model ? t("knowledge.create.keywordModel", { name: model.display_name }) : t("knowledge.create.keywordBm25");
});

const modelLabel = computed(() => {
  const model = props.options?.models.find((item) => item.id === props.kb.embedding_model_id);
  return model ? `${model.display_name} (${model.model_key}) · ${model.dimension}d` : `${props.kb.embedding_dim}d`;
});

async function save(section: "general" | "retrieval" | "chunking"): Promise<void> {
  saving.value = section;
  errors[section] = null;
  fieldErrors.value = {};
  try {
    if (section === "general") {
      await knowledge.update(props.kb.id, { name: general.name.trim(), description: general.description.trim() || null });
    } else if (section === "retrieval") {
      await knowledge.update(props.kb.id, {
        retrieval_config: {
          search_mode: retrieval.search_mode,
          top_k: Number(retrieval.top_k),
          candidate_k: Number(retrieval.candidate_k),
          score_threshold: Number(retrieval.score_threshold),
          expand_parent: retrieval.expand_parent,
          fusion: { method: retrieval.fusion, k: props.kb.retrieval_config.fusion?.k ?? 60 },
          weights: { dense: Number(retrieval.dense), sparse: Number(retrieval.sparse) },
          rerank: {
            enabled: retrieval.rerank_enabled,
            model_id: retrieval.rerank_model.trim() || null,
            top_n: Number(retrieval.rerank_top_n),
            timeout_s: props.kb.retrieval_config.rerank?.timeout_s ?? 1.5,
          },
          dedupe: props.kb.retrieval_config.dedupe ?? "none",
        },
      });
    } else {
      await knowledge.update(props.kb.id, {
        chunk_config: {
          strategy: chunking.strategy,
          child_tokens: Number(chunking.child_tokens),
          child_overlap: Number(chunking.child_overlap),
          parent_tokens: Number(chunking.parent_tokens),
          min_chunk_tokens: Number(chunking.min_chunk_tokens),
          keep_tables_intact: chunking.keep_tables_intact,
          prepend_heading_path: chunking.prepend_heading_path,
        },
      });
    }
    toasts.success(t("knowledge.settings.saved"));
    emit("saved");
  } catch (caught) {
    errors[section] = describeError(caught).message;
    if (caught instanceof ApiError) fieldErrors.value = caught.byField();
  } finally {
    saving.value = null;
  }
}
</script>

<template>
  <div data-test="kb-settings">
    <Section :title="t('knowledge.settings.general.title')" id="kb-general">
      <form class="grid max-w-xl gap-3" @submit.prevent="save('general')">
        <Notice v-if="errors.general" tone="bad">{{ errors.general }}</Notice>
        <Field :label="t('common.name')" required :error="fieldErrors.name" v-slot="{ id }">
          <Input :id="id" v-model="general.name" maxlength="255" data-test="kb-name" />
        </Field>
        <Field :label="t('common.description')" v-slot="{ id }">
          <Textarea :id="id" v-model="general.description" maxlength="4000" rows="3" />
        </Field>
        <dl class="grid grid-cols-2 gap-3 text-[12.5px] text-ink-2 sm:grid-cols-3">
          <div><dt class="text-ink-3">{{ t("knowledge.create.model") }}</dt><dd class="text-ink">{{ modelLabel }}</dd></div>
          <div><dt class="text-ink-3">{{ t("knowledge.settings.general.metric") }}</dt><dd class="text-ink">{{ kb.metric }}</dd></div>
          <div><dt class="text-ink-3">ID</dt><dd class="truncate font-mono text-[12px] text-ink" :title="kb.id">{{ kb.id }}</dd></div>
        </dl>
        <p class="text-[12px] text-ink-3">{{ t("knowledge.settings.general.modelFrozen") }}</p>
        <div><Button type="submit" variant="primary" :loading="saving === 'general'" :disabled="!general.name.trim()" data-test="save-general">{{ t("common.save") }}</Button></div>
      </form>
    </Section>

    <Section :title="t('knowledge.settings.retrieval.title')" :description="t('knowledge.settings.retrieval.description')" id="kb-retrieval">
      <form class="grid max-w-2xl gap-3" @submit.prevent="save('retrieval')">
        <Notice v-if="errors.retrieval" tone="bad">{{ errors.retrieval }}</Notice>
        <div class="grid gap-3 sm:grid-cols-3">
          <Field :label="t('knowledge.settings.retrieval.searchMode')" v-slot="{ id }">
            <NativeSelect :id="id" v-model="retrieval.search_mode" data-test="setting-search-mode">
              <option value="hybrid">{{ t("knowledge.search.modes.hybrid") }}</option>
              <option value="vector">{{ t("knowledge.search.modes.vector") }}</option>
              <option value="fulltext">{{ t("knowledge.search.modes.fulltext") }}</option>
            </NativeSelect>
          </Field>
          <Field :label="t('knowledge.search.topK')" v-slot="{ id }">
            <Input :id="id" v-model="retrieval.top_k" type="number" min="1" max="100" />
          </Field>
          <Field :label="t('knowledge.settings.retrieval.candidateK')" v-slot="{ id }">
            <Input :id="id" v-model="retrieval.candidate_k" type="number" min="1" max="1000" />
          </Field>
          <Field :label="t('knowledge.settings.retrieval.scoreThreshold')" v-slot="{ id }">
            <Input :id="id" v-model="retrieval.score_threshold" type="number" min="0" max="1" step="0.01" />
          </Field>
          <Field v-if="retrieval.search_mode === 'hybrid'" :label="t('knowledge.search.fusion')" v-slot="{ id }">
            <NativeSelect :id="id" v-model="retrieval.fusion">
              <option value="rrf">{{ t("knowledge.search.fusions.rrf") }}</option>
              <option value="weighted">{{ t("knowledge.search.fusions.weighted") }}</option>
            </NativeSelect>
          </Field>
          <template v-if="retrieval.search_mode === 'hybrid' && retrieval.fusion === 'weighted'">
            <Field :label="t('knowledge.search.denseWeight')" v-slot="{ id }">
              <Input :id="id" v-model="retrieval.dense" type="number" min="0" max="1" step="0.05" />
            </Field>
            <Field :label="t('knowledge.search.sparseWeight')" v-slot="{ id }">
              <Input :id="id" v-model="retrieval.sparse" type="number" min="0" max="1" step="0.05" />
            </Field>
          </template>
        </div>
        <Switch v-model="retrieval.expand_parent" :label="t('knowledge.settings.retrieval.expandParent')" data-test="setting-expand-parent" />
        <p class="-mt-1 text-[12px] text-ink-3">{{ t("knowledge.settings.retrieval.expandParentHint") }}</p>
        <Switch v-model="retrieval.rerank_enabled" :label="t('knowledge.settings.retrieval.rerank')" />
        <div v-if="retrieval.rerank_enabled" class="grid gap-3 sm:grid-cols-2">
          <Field :label="t('knowledge.search.rerankModel')" :hint="t('knowledge.settings.retrieval.rerankHint')" v-slot="{ id }">
            <Input :id="id" v-model="retrieval.rerank_model" placeholder="company-reranker" />
          </Field>
          <Field :label="t('knowledge.search.rerankTopN')" v-slot="{ id }">
            <Input :id="id" v-model="retrieval.rerank_top_n" type="number" min="1" max="100" />
          </Field>
        </div>
        <div><Button type="submit" variant="primary" :loading="saving === 'retrieval'" data-test="save-retrieval">{{ t("common.save") }}</Button></div>
      </form>
    </Section>

    <Section :title="t('knowledge.settings.keyword.title')" :description="t('knowledge.settings.keyword.description')" id="kb-keyword">
      <div class="grid gap-3">
        <p class="text-[13px] text-ink-2" data-test="keyword-current">
          {{ t("knowledge.settings.keyword.current") }} · <span class="font-medium text-ink">{{ keywordCurrent }}</span>
        </p>
        <div class="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
          <Field :label="t('knowledge.settings.keyword.next')" v-slot="{ id }">
            <NativeSelect :id="id" v-model="keywordNext" data-test="keyword-next">
              <option value="auto">{{ t("knowledge.create.keywordAuto", { choice: keywordAuto }) }}</option>
              <option value="bm25">{{ t("knowledge.create.keywordBm25") }}</option>
              <option v-for="model in sparseModels" :key="model.id" :value="`model:${model.id}`">
                {{ t("knowledge.create.keywordModel", { name: model.display_name }) }}
              </option>
            </NativeSelect>
          </Field>
          <Button variant="secondary" :disabled="building" data-test="keyword-rebuild" @click="emit('rebuild', parseSparseValue(keywordNext))">
            {{ t("knowledge.settings.keyword.rebuild") }}
          </Button>
        </div>
      </div>
    </Section>

    <Section :title="t('knowledge.settings.chunking.title')" :description="t('knowledge.settings.chunking.description')" id="kb-chunking">
      <form class="grid max-w-2xl gap-3" @submit.prevent="save('chunking')">
        <Notice v-if="errors.chunking" tone="bad">{{ errors.chunking }}</Notice>
        <div class="grid gap-3 sm:grid-cols-3">
          <Field :label="t('knowledge.settings.chunking.strategy')" v-slot="{ id }">
            <NativeSelect :id="id" v-model="chunking.strategy">
              <option v-for="strategy in ['parent_child', 'recursive', 'markdown', 'fixed', 'semantic']" :key="strategy" :value="strategy">
                {{ t(`knowledge.settings.chunking.strategies.${strategy}`) }}
              </option>
            </NativeSelect>
          </Field>
          <Field :label="t('knowledge.settings.chunking.childTokens')" v-slot="{ id }">
            <Input :id="id" v-model="chunking.child_tokens" type="number" min="32" max="8192" />
          </Field>
          <Field :label="t('knowledge.settings.chunking.childOverlap')" v-slot="{ id }">
            <Input :id="id" v-model="chunking.child_overlap" type="number" min="0" max="2048" />
          </Field>
          <Field v-if="chunking.strategy === 'parent_child'" :label="t('knowledge.settings.chunking.parentTokens')" v-slot="{ id }">
            <Input :id="id" v-model="chunking.parent_tokens" type="number" min="64" max="32768" />
          </Field>
          <Field :label="t('knowledge.settings.chunking.minChunkTokens')" v-slot="{ id }">
            <Input :id="id" v-model="chunking.min_chunk_tokens" type="number" min="1" max="4096" />
          </Field>
        </div>
        <Switch v-model="chunking.keep_tables_intact" :label="t('knowledge.settings.chunking.keepTables')" />
        <Switch v-model="chunking.prepend_heading_path" :label="t('knowledge.settings.chunking.prependHeadings')" />
        <div><Button type="submit" variant="primary" :loading="saving === 'chunking'" data-test="save-chunking">{{ t("common.save") }}</Button></div>
      </form>
    </Section>

    <Section :title="t('knowledge.settings.danger.title')" id="kb-danger">
      <div class="flex flex-wrap items-center justify-between gap-3 rounded-md border border-bad/30 px-3 py-3">
        <p class="max-w-[60ch] text-[13px] text-ink-2">{{ t("knowledge.settings.danger.description") }}</p>
        <Button variant="danger" size="sm" data-test="delete-kb-button" @click="emit('delete')">{{ t("knowledge.delete.action") }}</Button>
      </div>
    </Section>
  </div>
</template>
