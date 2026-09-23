<script setup lang="ts">
/**
 * One document: where it is in the pipeline, why it failed if it did, and
 * its chunks with in-place editing. Editing a chunk re-embeds that one
 * searchable vector; a parent chunk only changes the context returned with
 * its children, and the label says which is which.
 */
import { ArrowLeft, Download, Pencil, RotateCcw, Trash2 } from "lucide-vue-next";
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { knowledge, type ChunkRecord, type DocumentRecord, type KnowledgeBase } from "@/api/knowledge";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import KeyValue from "@/components/ui/KeyValue.vue";
import Notice from "@/components/ui/Notice.vue";
import ProgressBar from "@/components/ui/ProgressBar.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import StoneStack from "@/components/ui/StoneStack.vue";
import Textarea from "@/components/ui/Textarea.vue";
import { formatBytes, formatDateTime, formatNumber } from "@/lib/utils";
import { confirm } from "@/shared/composables/useConfirm";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError, isAbort } from "@/shared/errors";
import { PIPELINE_STAGES, documentTone, isTerminal, stageOf } from "@/shared/pipeline";

const props = defineProps<{ kb: KnowledgeBase; document: DocumentRecord; canWrite: boolean }>();
const emit = defineEmits<{ back: []; changed: [] }>();
const { t, locale } = useI18n();
const toasts = useToasts();

const chunks = ref<ChunkRecord[]>([]);
const chunksLoading = ref(false);
const chunksMore = ref(false);
const chunksError = ref<string | null>(null);
const editing = ref<string | null>(null);
const draft = ref("");
const saving = ref(false);
const busy = ref<"retry" | "delete" | null>(null);
let controller = new AbortController();

const stage = computed(() => stageOf(props.document.state));
const stageLabel = computed(() => {
  const info = stage.value;
  if (info.failed) return t("knowledge.documents.states.failed");
  const name = PIPELINE_STAGES[Math.min(info.filled, PIPELINE_STAGES.length - 1)]!;
  return t(`knowledge.documents.stages.${name}`);
});
const editable = computed(() => props.canWrite && !props.kb.building_index_version && props.document.state === "indexed");

const facts = computed(() => [
  { label: t("knowledge.documents.facts.type"), value: props.document.mime_type ?? "—", mono: true },
  { label: t("knowledge.documents.facts.size"), value: formatBytes(props.document.size_bytes) },
  { label: t("knowledge.documents.facts.pages"), value: props.document.page_count ?? "—" },
  { label: t("knowledge.columns.chunks"), value: formatNumber(props.document.chunk_count, locale.value) },
  { label: t("knowledge.documents.facts.revision"), value: props.document.revision },
  { label: t("knowledge.documents.facts.indexedAt"), value: formatDateTime(props.document.indexed_at, locale.value, "short") },
]);

async function loadChunks(more = false): Promise<void> {
  chunksLoading.value = true;
  chunksError.value = null;
  try {
    const page = await knowledge.chunks(
      props.kb.id,
      props.document.id,
      more ? chunks.value.at(-1)?.ordinal : undefined,
      controller.signal,
    );
    chunks.value = more ? [...chunks.value, ...page] : page;
    chunksMore.value = page.length === 100;
  } catch (caught) {
    if (!isAbort(caught)) chunksError.value = describeError(caught).message;
  } finally {
    chunksLoading.value = false;
  }
}

function startEdit(chunk: ChunkRecord): void {
  editing.value = chunk.id;
  draft.value = chunk.content;
}

async function saveEdit(): Promise<void> {
  if (!editing.value || !draft.value.trim()) return;
  saving.value = true;
  try {
    const updated = await knowledge.editChunk(props.kb.id, editing.value, draft.value);
    chunks.value = chunks.value.map((chunk) => (chunk.id === updated.id ? updated : chunk));
    editing.value = null;
    toasts.success(t("knowledge.chunks.saved"), t("knowledge.chunks.savedHint"));
    emit("changed");
  } catch (caught) {
    toasts.error(t("knowledge.chunks.saveFailed"), describeError(caught).message);
  } finally {
    saving.value = false;
  }
}

async function retry(): Promise<void> {
  busy.value = "retry";
  try {
    await knowledge.retry(props.kb.id, props.document.id);
    toasts.success(t("knowledge.documents.retried"));
    emit("changed");
  } catch (caught) {
    toasts.error(t("knowledge.documents.retryFailed"), describeError(caught).message);
  } finally {
    busy.value = null;
  }
}

async function remove(): Promise<void> {
  const accepted = await confirm({
    title: t("knowledge.documents.delete.title", { name: props.document.title ?? props.document.id }),
    description: t("knowledge.documents.delete.description"),
    confirmLabel: t("common.delete"),
    danger: true,
  });
  if (!accepted) return;
  busy.value = "delete";
  try {
    await knowledge.deleteDocument(props.kb.id, props.document.id);
    toasts.success(t("knowledge.documents.deleted"));
    emit("changed");
    emit("back");
  } catch (caught) {
    toasts.error(t("knowledge.documents.deleteFailed"), describeError(caught).message);
  } finally {
    busy.value = null;
  }
}

// Polling replaces the document object every few seconds; only a change in
// identity, state, revision or active index should reload the chunks.
watch(
  () => `${props.document.id}|${props.document.state}|${props.document.revision}|${props.kb.active_index_version}`,
  (_key, previous) => {
    if (previous && previous.split("|")[0] !== props.document.id) {
      controller.abort();
      controller = new AbortController();
      chunks.value = [];
      editing.value = null;
    }
    if (isTerminal(props.document.state) && props.document.state !== "failed") void loadChunks();
    else chunks.value = [];
  },
  { immediate: true },
);

onBeforeUnmount(() => controller.abort());
</script>

<template>
  <article class="rounded-md border border-line bg-surface" data-test="document-detail">
    <header class="border-b border-line px-4 py-3">
      <button type="button" class="mb-2 inline-flex items-center gap-1 text-[12px] text-ink-3 hover:text-ink lg:hidden" @click="emit('back')">
        <ArrowLeft class="size-3.5" aria-hidden="true" />
        {{ t("knowledge.tabs.documents") }}
      </button>
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="flex min-w-0 items-start gap-3">
          <StoneStack :state="document.state" :size="28" class="mt-0.5" />
          <div class="min-w-0">
            <h3 class="truncate text-[15px] font-semibold text-ink" data-test="doc-title">{{ document.title ?? document.id }}</h3>
            <p class="mt-0.5 flex flex-wrap items-center gap-2 text-[12.5px] text-ink-2">
              <Badge :tone="documentTone(document.state)" size="sm" dot :pulse="!isTerminal(document.state)">{{ t(`knowledge.documents.states.${document.state}`) }}</Badge>
              <span v-if="!isTerminal(document.state)">{{ stageLabel }}</span>
              <span v-if="document.stage_detail" class="truncate text-ink-3">{{ document.stage_detail }}</span>
            </p>
          </div>
        </div>
        <div class="flex shrink-0 flex-wrap items-center gap-1.5">
          <Button v-if="document.state === 'failed' && canWrite" size="sm" variant="secondary" :loading="busy === 'retry'" data-test="retry-doc" @click="retry">
            <RotateCcw aria-hidden="true" />
            {{ t("knowledge.documents.retry") }}
          </Button>
          <Button size="sm" variant="outline" :href="knowledge.download(kb.id, document.id)" target="_blank" rel="noopener" data-test="download-doc">
            <Download aria-hidden="true" />
            {{ t("knowledge.documents.download") }}
          </Button>
          <Button v-if="canWrite" size="sm" variant="ghost" :disabled="document.state === 'deleting'" :loading="busy === 'delete'" data-test="delete-doc" @click="remove">
            <Trash2 aria-hidden="true" />
            {{ t("common.delete") }}
          </Button>
        </div>
      </div>
      <ProgressBar v-if="!isTerminal(document.state)" :value="document.progress_pct" class="mt-3" :label="t('knowledge.documents.progress')" />
    </header>

    <Notice v-if="document.state === 'failed'" tone="bad" :title="document.error_code ?? t('knowledge.documents.states.failed')" class="m-4" test-id="doc-error">
      {{ document.error_detail ?? t("knowledge.documents.failedHint") }}
    </Notice>

    <KeyValue :items="facts" :columns="3" class="px-4 py-3" />

    <section class="border-t border-line px-4 py-3" :aria-label="t('knowledge.chunks.title')">
      <div class="mb-2 flex items-center justify-between gap-3">
        <h4 class="text-[13px] font-semibold text-ink">{{ t("knowledge.chunks.title") }}</h4>
        <p v-if="kb.building_index_version" class="text-[12px] text-ink-3">{{ t("knowledge.chunks.lockedDuringBuild") }}</p>
      </div>

      <ErrorState v-if="chunksError" :message="chunksError" compact @retry="loadChunks()" />
      <Skeleton v-else-if="chunksLoading && !chunks.length" :rows="3" />
      <p v-else-if="!isTerminal(document.state)" class="text-[12.5px] text-ink-3">{{ t("knowledge.chunks.pending") }}</p>
      <EmptyState v-else-if="!chunks.length" :title="t('knowledge.chunks.empty')" compact />

      <ol v-else class="grid gap-2" data-test="chunk-list">
        <li v-for="chunk in chunks" :key="chunk.id" class="chunk rounded-md border border-line" :data-test="`chunk-${chunk.id}`">
          <div class="flex flex-wrap items-center gap-2 border-b border-line bg-surface-2 px-3 py-1.5 text-[11.5px] text-ink-3">
            <span class="tnum font-mono">#{{ chunk.ordinal }}</span>
            <Badge size="sm" :tone="chunk.metadata.embed === false ? 'neutral' : 'brand'">
              {{ chunk.metadata.embed === false ? t("knowledge.chunks.parent") : t("knowledge.chunks.searchable") }}
            </Badge>
            <span class="tnum">{{ t("knowledge.chunks.tokens", { n: chunk.token_count }) }}</span>
            <span v-if="typeof chunk.metadata.page === 'number'" class="tnum">{{ t("knowledge.chunks.page", { n: chunk.metadata.page }) }}</span>
            <span v-if="Array.isArray(chunk.metadata.heading_path) && chunk.metadata.heading_path.length" class="min-w-0 truncate" :title="(chunk.metadata.heading_path as string[]).join(' › ')">
              {{ (chunk.metadata.heading_path as string[]).join(" › ") }}
            </span>
            <Badge v-if="chunk.is_edited" size="sm" tone="warn">{{ t("knowledge.chunks.edited") }}</Badge>
            <span class="flex-1" />
            <Button v-if="editable && editing !== chunk.id" size="sm" variant="ghost" data-test="edit-chunk" @click="startEdit(chunk)">
              <Pencil aria-hidden="true" />
              {{ t("common.edit") }}
            </Button>
          </div>
          <div v-if="editing === chunk.id" class="grid gap-2 p-3">
            <Textarea v-model="draft" rows="8" data-test="chunk-editor" />
            <p class="text-[12px] text-ink-3">
              {{ chunk.metadata.embed === false ? t("knowledge.chunks.parentEditHint") : t("knowledge.chunks.childEditHint") }}
            </p>
            <div class="flex justify-end gap-2">
              <Button size="sm" variant="ghost" @click="editing = null">{{ t("common.cancel") }}</Button>
              <Button size="sm" variant="primary" :loading="saving" :disabled="!draft.trim()" data-test="save-chunk" @click="saveEdit">{{ t("common.save") }}</Button>
            </div>
          </div>
          <pre v-else class="whitespace-pre-wrap break-words px-3 py-2.5 font-sans text-[13px] leading-relaxed text-ink">{{ chunk.content }}</pre>
        </li>
      </ol>
      <div v-if="chunksMore" class="mt-2 text-center">
        <Button variant="ghost" size="sm" :loading="chunksLoading" @click="loadChunks(true)">{{ t("common.loadMore") }}</Button>
      </div>
    </section>
  </article>
</template>
