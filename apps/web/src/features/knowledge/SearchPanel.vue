<script setup lang="ts">
/**
 * Search console. Runs the same public query an agent would run, with the
 * knobs a person needs to judge quality: mode, fusion, parent context,
 * reranking, filters, deduplication and diversity. Results cite the document
 * and page so a bad hit can be traced back to its chunk in one click; Explain
 * shows what each stage did, and the request can be copied as curl.
 */
import { ArrowRight, Search, SlidersHorizontal } from "lucide-vue-next";
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";

import {
  buildQuery,
  curlFor,
  FilterSyntaxError,
  knowledge,
  type DocumentRecord,
  type KnowledgeBase,
  type QueryOptions,
  type RetrievalResult,
} from "@/api/knowledge";
import { meta } from "@/api/meta";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import Checkbox from "@/components/ui/Checkbox.vue";
import CopyButton from "@/components/ui/CopyButton.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import NativeSelect from "@/components/ui/NativeSelect.vue";
import Notice from "@/components/ui/Notice.vue";
import SegmentedControl from "@/components/ui/SegmentedControl.vue";
import Switch from "@/components/ui/Switch.vue";
import Textarea from "@/components/ui/Textarea.vue";
import { shortId } from "@/lib/utils";
import { describeError, isAbort } from "@/shared/errors";

import ExplainPanel from "./ExplainPanel.vue";

const props = defineProps<{ kb: KnowledgeBase; documents: DocumentRecord[] }>();
const { t } = useI18n();
const route = useRoute();
const router = useRouter();

const query = ref(typeof route.query.q === "string" ? route.query.q : "");
const searching = ref(false);
const result = ref<RetrievalResult | null>(null);
const error = ref<{ message: string; requestId?: string } | null>(null);
const advanced = ref(false);
const rerankAvailable = ref(false);
const input = ref<InstanceType<typeof Input>>();
let controller: AbortController | null = null;

const config = props.kb.retrieval_config;
const options = reactive<QueryOptions>({
  mode: config.search_mode ?? "hybrid",
  topK: config.top_k ?? 5,
  fusion: config.fusion?.method ?? "rrf",
  dense: config.weights?.dense ?? 0.7,
  sparse: config.weights?.sparse ?? 0.3,
  expandParent: config.expand_parent ?? false,
  rerank: false,
  rerankModel: config.rerank?.model_id ?? "",
  rerankTopN: config.rerank?.top_n ?? 5,
  metadataFilter: "",
  documentIds: [],
  createdAfter: "",
  createdBefore: "",
  dedupe: config.dedupe === "by_document" ? "by_document" : "none",
  mmr: config.mmr?.enabled ?? false,
  mmrLambda: config.mmr?.lambda ?? 0.5,
  highlights: true,
  explain: false,
});
const filterError = ref<string | null>(null);
const OPERATORS = "$eq, $ne, $in, $nin, $gt, $gte, $lt, $lte, $exists, $and, $or, $not";
const METADATA_EXAMPLE = '{"lang": "en", "tags": {"$in": ["ops"]}}';

const curl = computed(() => {
  const text = query.value.trim();
  if (!text) return "";
  try {
    return curlFor(buildQuery(props.kb.id, text, options), window.location.origin);
  } catch {
    return "";
  }
});

const modes = computed(() => [
  { value: "hybrid" as const, label: t("knowledge.search.modes.hybrid") },
  { value: "vector" as const, label: t("knowledge.search.modes.vector") },
  { value: "fulltext" as const, label: t("knowledge.search.modes.fulltext") },
]);

const titles = computed(() => new Map(props.documents.map((doc) => [doc.id, doc.title ?? doc.id])));
const notices = computed(() => [
  ...(result.value?.degraded ?? []).map((item) => ({ key: `d:${item.stage}:${item.reason}`, text: item.detail, tone: "warn" as const })),
  ...(result.value?.partial_failures ?? []).map((item) => ({ key: `f:${item.knowledge_base_id}:${item.code}`, text: item.detail, tone: "bad" as const })),
]);
const latency = computed(() => {
  const values = result.value?.usage.latency_ms;
  if (!values) return null;
  return Object.values(values).reduce((sum, ms) => sum + ms, 0);
});

async function search(): Promise<void> {
  const text = query.value.trim();
  if (!text) return;
  controller?.abort();
  controller = new AbortController();
  searching.value = true;
  error.value = null;
  filterError.value = null;
  void router.replace({ query: { ...route.query, q: text } });
  try {
    result.value = await knowledge.query(props.kb.id, text, options, controller.signal);
  } catch (caught) {
    if (isAbort(caught)) return;
    if (caught instanceof FilterSyntaxError) {
      advanced.value = true;
      filterError.value = t("knowledge.search.metadataFilterInvalid");
      return;
    }
    const failure = describeError(caught);
    error.value = failure.requestId ? { message: failure.message, requestId: failure.requestId } : { message: failure.message };
  } finally {
    searching.value = false;
  }
}

function openDocument(documentId: string): void {
  void router.push({ query: { ...route.query, tab: undefined, doc: documentId } });
}

function pageOf(metadata: Record<string, unknown> | undefined): number | null {
  const page = metadata?.page;
  return typeof page === "number" ? page : null;
}

/** Content split into plain and highlighted runs; spans are code-point offsets. */
function segments(content: string, spans: Record<string, number>[] | undefined): { text: string; mark: boolean }[] {
  if (!spans?.length) return [{ text: content, mark: false }];
  const chars = Array.from(content);
  const parts: { text: string; mark: boolean }[] = [];
  let cursor = 0;
  for (const span of spans) {
    const start = span.start ?? cursor;
    const end = span.end ?? start;
    if (start > cursor) parts.push({ text: chars.slice(cursor, start).join(""), mark: false });
    parts.push({ text: chars.slice(start, end).join(""), mark: true });
    cursor = end;
  }
  if (cursor < chars.length) parts.push({ text: chars.slice(cursor).join(""), mark: false });
  return parts;
}

function formatDate(value: string | null | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleDateString();
}

function headingOf(metadata: Record<string, unknown> | undefined): string | null {
  const path = metadata?.heading_path;
  return Array.isArray(path) && path.length ? path.join(" › ") : null;
}

onMounted(async () => {
  try {
    rerankAvailable.value = (await meta()).capabilities.rerank === true;
  } catch {
    rerankAvailable.value = false;
  }
  if (query.value) void search();
});

onBeforeUnmount(() => controller?.abort());
</script>

<template>
  <div class="grid gap-4" data-test="search-panel">
    <form class="grid gap-3" @submit.prevent="search">
      <div class="flex flex-col gap-2 sm:flex-row">
        <Input
          ref="input"
          v-model="query"
          class="h-10 flex-1 text-[14px]"
          :placeholder="t('knowledge.search.placeholder')"
          maxlength="8192"
          autofocus
          data-test="retrieval-query"
        >
          <template #prefix><Search /></template>
        </Input>
        <Button type="submit" variant="primary" size="lg" :loading="searching" :disabled="!query.trim()" data-test="run-search">
          {{ t("knowledge.search.run") }}
        </Button>
      </div>
      <div class="flex flex-wrap items-center gap-3">
        <SegmentedControl v-model="options.mode" :options="modes" size="sm" :aria-label="t('knowledge.search.mode')" />
        <Switch v-model="options.expandParent" :label="t('knowledge.search.expandParent')" data-test="expand-parent" />
        <Button type="button" variant="ghost" size="sm" :aria-expanded="advanced" data-test="toggle-advanced" @click="advanced = !advanced">
          <SlidersHorizontal aria-hidden="true" />
          {{ t("knowledge.search.advanced") }}
        </Button>
      </div>
      <div v-if="advanced" class="grid gap-3 rounded-md border border-line bg-surface-2 p-3 sm:grid-cols-2 lg:grid-cols-4" data-test="advanced-options">
        <Field :label="t('knowledge.search.topK')" v-slot="{ id }">
          <Input :id="id" v-model="options.topK" type="number" min="1" max="50" size="sm" />
        </Field>
        <Field v-if="options.mode === 'hybrid'" :label="t('knowledge.search.fusion')" v-slot="{ id }">
          <NativeSelect :id="id" v-model="options.fusion" size="sm">
            <option value="rrf">{{ t("knowledge.search.fusions.rrf") }}</option>
            <option value="weighted">{{ t("knowledge.search.fusions.weighted") }}</option>
          </NativeSelect>
        </Field>
        <template v-if="options.mode === 'hybrid' && options.fusion === 'weighted'">
          <Field :label="t('knowledge.search.denseWeight')" v-slot="{ id }">
            <Input :id="id" v-model="options.dense" type="number" min="0" max="1" step="0.05" size="sm" />
          </Field>
          <Field :label="t('knowledge.search.sparseWeight')" v-slot="{ id }">
            <Input :id="id" v-model="options.sparse" type="number" min="0" max="1" step="0.05" size="sm" />
          </Field>
        </template>
        <div class="grid gap-2 sm:col-span-2">
          <Checkbox v-model="options.rerank" :disabled="!rerankAvailable" data-test="rerank-toggle">
            {{ t("knowledge.search.rerank") }}
            <span v-if="!rerankAvailable" class="text-ink-3"> — {{ t("knowledge.search.rerankUnavailable") }}</span>
          </Checkbox>
          <div v-if="options.rerank" class="grid gap-3 sm:grid-cols-2">
            <Field :label="t('knowledge.search.rerankModel')" v-slot="{ id }">
              <Input :id="id" v-model="options.rerankModel" size="sm" placeholder="company-reranker" />
            </Field>
            <Field :label="t('knowledge.search.rerankTopN')" v-slot="{ id }">
              <Input :id="id" v-model="options.rerankTopN" type="number" min="1" max="50" size="sm" />
            </Field>
          </div>
        </div>
        <fieldset class="grid gap-2 sm:col-span-2 lg:col-span-4" data-test="ranking-options">
          <legend class="mb-1 text-[12px] font-medium text-ink-3">{{ t("knowledge.search.ranking") }}</legend>
          <div class="flex flex-wrap items-center gap-x-5 gap-y-2">
            <Checkbox
              :model-value="options.dedupe === 'by_document'"
              data-test="dedupe-toggle"
              @update:model-value="options.dedupe = $event === true ? 'by_document' : 'none'"
            >
              {{ t("knowledge.search.dedupeByDocument") }}
            </Checkbox>
            <Checkbox v-model="options.mmr" data-test="mmr-toggle">{{ t("knowledge.search.mmr") }}</Checkbox>
            <Field v-if="options.mmr" :label="t('knowledge.search.mmrLambda')" v-slot="{ id }" class="w-36">
              <Input :id="id" v-model="options.mmrLambda" type="number" min="0" max="1" step="0.1" size="sm" />
            </Field>
            <Checkbox v-model="options.highlights" data-test="highlight-toggle">{{ t("knowledge.search.highlights") }}</Checkbox>
            <Checkbox v-model="options.explain" data-test="explain-toggle">{{ t("knowledge.search.explain") }}</Checkbox>
          </div>
        </fieldset>
        <fieldset class="grid gap-3 sm:col-span-2 sm:grid-cols-2 lg:col-span-4 lg:grid-cols-4" data-test="filter-options">
          <legend class="mb-1 text-[12px] font-medium text-ink-3">{{ t("knowledge.search.filters") }}</legend>
          <Field
            :label="t('knowledge.search.metadataFilter')"
            :hint="t('knowledge.search.metadataFilterHint', { ops: OPERATORS })"
            :error="filterError ?? undefined"
            class="sm:col-span-2"
            v-slot="{ id }"
          >
            <Textarea :id="id" v-model="options.metadataFilter" mono :invalid="!!filterError" :placeholder="METADATA_EXAMPLE" data-test="metadata-filter" />
          </Field>
          <Field :label="t('knowledge.search.documentsFilter')" v-slot="{ id }">
            <select
              :id="id"
              v-model="options.documentIds"
              multiple
              class="h-24 w-full rounded-sm border border-line-strong bg-surface px-2 py-1 text-[12.5px] text-ink"
              data-test="documents-filter"
            >
              <option v-for="document in documents" :key="document.id" :value="document.id">{{ document.title ?? document.id }}</option>
            </select>
          </Field>
          <div class="grid gap-3">
            <Field :label="t('knowledge.search.createdAfter')" v-slot="{ id }">
              <Input :id="id" v-model="options.createdAfter" type="datetime-local" size="sm" data-test="created-after" />
            </Field>
            <Field :label="t('knowledge.search.createdBefore')" v-slot="{ id }">
              <Input :id="id" v-model="options.createdBefore" type="datetime-local" size="sm" />
            </Field>
          </div>
        </fieldset>
      </div>
    </form>

    <ErrorState v-if="error" :message="error.message" :request-id="error.requestId" :retryable="false" />

    <template v-if="result">
      <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12.5px] text-ink-3" data-test="search-summary">
        <span>{{ t("knowledge.search.summary", { n: result.results.length }) }}</span>
        <span v-if="latency !== null" class="tnum">{{ t("knowledge.search.latency", { ms: latency }) }}</span>
        <span v-if="result.usage.embedding_tokens" class="tnum">{{ t("knowledge.search.embeddingTokens", { n: result.usage.embedding_tokens }) }}</span>
        <span v-if="result.truncated_to_token_budget">{{ t("knowledge.search.truncated") }}</span>
        <span class="flex-1" />
        <span v-if="curl" class="inline-flex items-center gap-1" :title="t('knowledge.search.curlHint')" data-test="copy-curl">
          <CopyButton :value="curl" :label="t('knowledge.search.copyCurl')" size="sm" />
        </span>
      </div>
      <Notice v-for="notice in notices" :key="notice.key" :tone="notice.tone" test-id="search-notice">{{ notice.text }}</Notice>
      <ExplainPanel v-if="result.explain" :explain="result.explain" />

      <EmptyState v-if="!result.results.length" :title="t('knowledge.search.noResults')" :description="t('knowledge.search.noResultsHint')" compact />

      <ol v-else class="grid gap-2" data-test="search-results">
        <li v-for="(hit, index) in result.results" :key="hit.chunk_id" class="rounded-md border border-line bg-surface">
          <div class="flex flex-wrap items-center gap-2 border-b border-line px-3 py-1.5 text-[11.5px] text-ink-3">
            <span class="tnum font-display text-[13px] font-semibold text-ink">{{ index + 1 }}</span>
            <Badge tone="brand" size="sm" class="tnum">{{ t("knowledge.search.score") }} {{ hit.score.toFixed(3) }}</Badge>
            <button type="button" class="min-w-0 max-w-[40ch] truncate text-ink-2 underline-offset-2 hover:text-brand hover:underline" :title="titles.get(hit.document_id) ?? hit.document_id" @click="openDocument(hit.document_id)">
              {{ titles.get(hit.document_id) ?? shortId(hit.document_id) }}
            </button>
            <span v-if="pageOf(hit.metadata)" class="tnum">{{ t("knowledge.chunks.page", { n: pageOf(hit.metadata) }) }}</span>
            <span v-if="headingOf(hit.metadata)" class="min-w-0 truncate">{{ headingOf(hit.metadata) }}</span>
            <span v-if="hit.matched_child_ids?.length" class="tnum">{{ t("knowledge.search.matchedChildren", { n: hit.matched_child_ids.length }) }}</span>
            <span v-if="formatDate(hit.source?.updated_at)" class="tnum">{{ t("knowledge.search.updated", { date: formatDate(hit.source?.updated_at) }) }}</span>
            <span class="flex-1" />
            <Button size="sm" variant="ghost" @click="openDocument(hit.document_id)">
              {{ t("knowledge.search.openDocument") }}
              <ArrowRight aria-hidden="true" />
            </Button>
          </div>
          <pre class="whitespace-pre-wrap break-words px-3 py-2.5 font-sans text-[13px] leading-relaxed text-ink" data-test="hit-content"><template
            v-for="(part, partIndex) in segments(hit.content, hit.highlights)"
            :key="partIndex"
          ><mark v-if="part.mark" class="rounded-[2px] bg-warn/25 px-px text-ink">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></pre>
        </li>
      </ol>
    </template>
    <p v-else-if="!searching" class="text-[12.5px] text-ink-3">{{ t("knowledge.search.hint") }}</p>
  </div>
</template>
