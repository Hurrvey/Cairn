<script setup lang="ts">
/**
 * Documents: list on the left, the selected document on the right.
 *
 * The list is the working surface — filter by state, drop files, see each
 * document's position in the pipeline at a glance. Selection is kept in the
 * URL (`?doc=`) so a link to a failed document opens straight onto it.
 */
import { FileText, Search, Upload } from "lucide-vue-next";
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";

import { knowledge, type DocumentRecord, type DocumentRegistration, type KnowledgeBase, type SetupOptions } from "@/api/knowledge";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import Input from "@/components/ui/Input.vue";
import SegmentedControl from "@/components/ui/SegmentedControl.vue";
import StoneStack from "@/components/ui/StoneStack.vue";
import DocumentDetail from "@/features/knowledge/DocumentDetail.vue";
import { cn, formatBytes, formatRelative } from "@/lib/utils";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError } from "@/shared/errors";
import { documentTone, isTerminal } from "@/shared/pipeline";

type StateFilter = "all" | "processing" | "indexed" | "failed";

const props = defineProps<{
  kb: KnowledgeBase;
  documents: DocumentRecord[];
  hasMore: boolean;
  options: SetupOptions | undefined;
  canWrite: boolean;
}>();
const emit = defineEmits<{ refresh: []; loadMore: [] }>();

const { t, locale } = useI18n();
const route = useRoute();
const router = useRouter();
const toasts = useToasts();

const filter = ref("");
const fileInput = ref<HTMLInputElement>();
const dragging = ref(false);
const uploads = ref<{ name: string; status: "uploading" | DocumentRegistration["status"] | "error"; reason?: string }[]>([]);
const uploading = ref(false);

const stateFilter = computed<StateFilter>({
  get: () => {
    const value = route.query.state;
    return value === "processing" || value === "indexed" || value === "failed" ? value : "all";
  },
  set: (value) => {
    void router.replace({ query: { ...route.query, state: value === "all" ? undefined : value } });
  },
});

const selectedId = computed(() => (typeof route.query.doc === "string" ? route.query.doc : null));
const selected = computed(() => props.documents.find((doc) => doc.id === selectedId.value) ?? null);

const counts = computed(() => ({
  all: props.documents.length,
  processing: props.documents.filter((doc) => !isTerminal(doc.state)).length,
  indexed: props.documents.filter((doc) => doc.state === "indexed").length,
  failed: props.documents.filter((doc) => doc.state === "failed").length,
}));

const segments = computed(() => [
  { value: "all" as StateFilter, label: t("knowledge.documents.filters.all"), count: counts.value.all },
  { value: "processing" as StateFilter, label: t("knowledge.documents.filters.processing"), count: counts.value.processing },
  { value: "indexed" as StateFilter, label: t("knowledge.documents.filters.indexed"), count: counts.value.indexed },
  { value: "failed" as StateFilter, label: t("knowledge.documents.filters.failed"), count: counts.value.failed, testId: "filter-failed" },
]);

const visible = computed(() => {
  const needle = filter.value.trim().toLowerCase();
  return props.documents.filter((doc) => {
    if (stateFilter.value === "processing" && isTerminal(doc.state)) return false;
    if (stateFilter.value === "indexed" && doc.state !== "indexed") return false;
    if (stateFilter.value === "failed" && doc.state !== "failed") return false;
    if (needle && !(doc.title ?? doc.id).toLowerCase().includes(needle)) return false;
    return true;
  });
});

const accept = computed(
  () =>
    props.options?.supported_extensions.join(",") ??
    ".md,.txt,.html,.json,.docx,.pptx,.xlsx,.csv,.pdf,.png,.jpg,.jpeg,.tif,.tiff",
);

function select(doc: DocumentRecord | null): void {
  void router.replace({ query: { ...route.query, doc: doc?.id } });
}

function openFilePicker(): void {
  fileInput.value?.click();
}

async function uploadFiles(files: File[]): Promise<void> {
  if (!files.length || !props.canWrite) return;
  uploading.value = true;
  uploads.value = files.map((file) => ({ name: file.name, status: "uploading" }));
  let accepted = 0;
  for (const [index, file] of files.entries()) {
    const entry = uploads.value[index]!;
    if (props.options && file.size > props.options.max_upload_bytes) {
      entry.status = "rejected";
      entry.reason = t("knowledge.documents.tooLarge", { limit: formatBytes(props.options.max_upload_bytes) });
      continue;
    }
    try {
      const result = await knowledge.upload(props.kb.id, file);
      entry.status = result.status;
      if (result.reason) entry.reason = result.reason;
      if (result.status === "accepted") accepted += 1;
    } catch (caught) {
      entry.status = "error";
      entry.reason = describeError(caught).message;
    }
  }
  uploading.value = false;
  if (accepted) toasts.success(t("knowledge.documents.uploaded", { n: accepted }));
  emit("refresh");
  window.setTimeout(() => {
    if (!uploading.value) uploads.value = uploads.value.filter((entry) => entry.status !== "accepted");
  }, 6000);
}

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = "";
  void uploadFiles(files);
}

function onDrop(event: DragEvent): void {
  dragging.value = false;
  const files = Array.from(event.dataTransfer?.files ?? []);
  void uploadFiles(files);
}

defineExpose({ openFilePicker });
</script>

<template>
  <div
    :class="cn('grid gap-4 lg:grid-cols-[minmax(300px,380px)_minmax(0,1fr)]', selected && 'max-lg:[&>.list]:hidden')"
    data-test="documents-panel"
    @dragover.prevent="canWrite && (dragging = true)"
    @dragleave.prevent="dragging = false"
    @drop.prevent="onDrop"
  >
    <!-- List -->
    <section class="list min-w-0" :aria-label="t('knowledge.tabs.documents')">
      <div class="mb-2 flex flex-wrap items-center gap-2">
        <Input v-model="filter" class="min-w-40 flex-1" size="sm" :placeholder="t('knowledge.documents.filter')" type="search" data-test="doc-filter">
          <template #prefix><Search /></template>
        </Input>
      </div>
      <SegmentedControl v-model="stateFilter" :options="segments" size="sm" class="mb-3 w-full [&>button]:flex-1" :aria-label="t('knowledge.documents.filters.label')" />

      <div
        v-if="canWrite"
        :class="
          cn(
            'mb-3 flex items-center gap-3 rounded-md border border-dashed px-3 py-2.5 text-[12.5px] text-ink-2 transition-colors',
            dragging ? 'border-brand bg-brand-soft/60' : 'border-line-strong',
          )
        "
        data-test="dropzone"
      >
        <Upload class="size-4 shrink-0 text-ink-3" aria-hidden="true" />
        <span class="min-w-0 flex-1">
          {{ t("knowledge.documents.dropHint") }}
          <button type="button" class="text-brand underline-offset-2 hover:underline" @click="openFilePicker">{{ t("knowledge.documents.browse") }}</button>
        </span>
        <input ref="fileInput" type="file" multiple class="sr-only" :accept="accept" data-test="document-upload" @change="onFileChange" />
      </div>

      <ul v-if="uploads.length" class="mb-3 divide-y divide-line rounded-md border border-line text-[12.5px]" data-test="upload-queue">
        <li v-for="entry in uploads" :key="entry.name" class="flex items-center gap-2 px-3 py-1.5">
          <span class="min-w-0 flex-1 truncate">{{ entry.name }}</span>
          <Badge
            size="sm"
            :tone="entry.status === 'accepted' ? 'ok' : entry.status === 'uploading' ? 'info' : entry.status === 'skipped' ? 'neutral' : 'bad'"
            :dot="entry.status === 'uploading'"
            :pulse="entry.status === 'uploading'"
          >
            {{ t(`knowledge.documents.uploadStatus.${entry.status}`) }}
          </Badge>
          <span v-if="entry.reason" class="max-w-40 truncate text-ink-3" :title="entry.reason">{{ entry.reason }}</span>
        </li>
      </ul>

      <EmptyState
        v-if="!documents.length"
        :title="t('knowledge.documents.empty.title')"
        :description="canWrite ? t('knowledge.documents.empty.description') : t('knowledge.documents.empty.readOnly')"
        compact
      >
        <template #icon><FileText /></template>
        <Button v-if="canWrite" variant="primary" size="sm" @click="openFilePicker">{{ t("knowledge.documents.upload") }}</Button>
      </EmptyState>
      <EmptyState v-else-if="!visible.length" :title="t('knowledge.documents.noMatch')" compact />

      <ul v-else class="divide-y divide-line overflow-hidden rounded-md border border-line" role="list" data-test="document-list">
        <li v-for="doc in visible" :key="doc.id">
          <button
            type="button"
            :class="
              cn(
                'flex w-full items-center gap-3 px-3 py-2 text-left transition-colors outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand/40',
                selectedId === doc.id ? 'bg-brand-soft/60' : 'hover:bg-surface-2',
              )
            "
            :aria-current="selectedId === doc.id ? 'true' : undefined"
            :data-test="`doc-row-${doc.id}`"
            :data-state="doc.state"
            @click="select(doc)"
          >
            <StoneStack :state="doc.state" :size="20" :title="t(`knowledge.documents.states.${doc.state}`)" />
            <span class="min-w-0 flex-1">
              <span class="block truncate text-[13px] font-medium text-ink">{{ doc.title ?? doc.id }}</span>
              <span class="mt-0.5 flex items-center gap-2 text-[11.5px] text-ink-3">
                <Badge :tone="documentTone(doc.state)" size="sm">{{ t(`knowledge.documents.states.${doc.state}`) }}</Badge>
                <span v-if="!isTerminal(doc.state)" class="tnum">{{ doc.progress_pct }}%</span>
                <span class="truncate">{{ formatRelative(doc.indexed_at ?? doc.created_at, locale) }}</span>
              </span>
            </span>
          </button>
        </li>
      </ul>
      <div v-if="hasMore" class="mt-2 text-center">
        <Button variant="ghost" size="sm" data-test="load-more-docs" @click="emit('loadMore')">{{ t("common.loadMore") }}</Button>
      </div>
    </section>

    <!-- Detail -->
    <section class="min-w-0" :aria-label="t('knowledge.documents.detail')">
      <DocumentDetail
        v-if="selected"
        :kb="kb"
        :document="selected"
        :can-write="canWrite"
        @back="select(null)"
        @changed="emit('refresh')"
      />
      <div
        v-else
        class="hidden h-full min-h-[280px] items-center justify-center rounded-md border border-dashed border-line text-center text-[13px] text-ink-3 lg:flex"
        data-test="no-selection"
      >
        <p class="max-w-xs px-6">{{ documents.length ? t("knowledge.documents.selectHint") : t("knowledge.documents.selectHintEmpty") }}</p>
      </div>
    </section>
  </div>
</template>
