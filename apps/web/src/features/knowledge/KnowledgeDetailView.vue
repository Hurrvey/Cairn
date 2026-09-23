<script setup lang="ts">
/**
 * Knowledge base detail: the page a contributor lives on.
 *
 * The view owns the data (base, documents, counts, index progress) and the
 * polling cadence; the panels below render it and ask for a refresh when
 * they change something. Polling runs fast while work is in flight and slows
 * down when the base is idle, so an open tab is neither stale nor noisy.
 */
import { MoreHorizontal, RefreshCw, Search, Upload } from "lucide-vue-next";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";

import { ApiError } from "@/api/client";
import {
  knowledge,
  type DocumentRecord,
  type DocumentStateCounts,
  type IndexProgress,
  type KnowledgeBase,
  type SetupOptions,
} from "@/api/knowledge";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import DropdownItem from "@/components/ui/DropdownItem.vue";
import DropdownMenu from "@/components/ui/DropdownMenu.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import Notice from "@/components/ui/Notice.vue";
import ProgressBar from "@/components/ui/ProgressBar.vue";
import SegmentedControl from "@/components/ui/SegmentedControl.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import BreakGlassDialog from "@/features/knowledge/BreakGlassDialog.vue";
import DocumentsPanel from "@/features/knowledge/DocumentsPanel.vue";
import KbSettingsPanel from "@/features/knowledge/KbSettingsPanel.vue";
import SearchPanel from "@/features/knowledge/SearchPanel.vue";
import VersionsPanel from "@/features/knowledge/VersionsPanel.vue";
import { formatBytes, formatDuration, formatNumber, formatRelative } from "@/lib/utils";
import PageHeader from "@/shared/components/PageHeader.vue";
import { confirm } from "@/shared/composables/useConfirm";
import { useKnowledgePermissions } from "@/shared/composables/useKnowledgePermissions";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError, isAbort } from "@/shared/errors";
import { isTerminal, kbTone } from "@/shared/pipeline";

type Tab = "documents" | "search" | "versions" | "settings";

const ACTIVE_POLL_MS = 3000;
const IDLE_POLL_MS = 12000;

const route = useRoute();
const router = useRouter();
const { t, locale } = useI18n();
const permissions = useKnowledgePermissions();
const toasts = useToasts();

const id = computed(() => String(route.params.id));
const kb = ref<KnowledgeBase>();
const documents = ref<DocumentRecord[]>([]);
const cursor = ref<string | null>(null);
const stats = ref<DocumentStateCounts | null>(null);
const progress = ref<IndexProgress | null>(null);
const options = ref<SetupOptions>();
const loading = ref(true);
const error = ref<{ message: string; requestId?: string } | null>(null);
const accessBlocked = ref(false);
const breakGlassOpen = ref(false);
const documentsPanel = ref<InstanceType<typeof DocumentsPanel>>();

let controller = new AbortController();
let timer: ReturnType<typeof setTimeout> | undefined;
let disposed = false;

const tab = computed<Tab>({
  get: () => {
    const value = route.query.tab;
    return value === "search" || value === "versions" || value === "settings" ? value : "documents";
  },
  set: (value) => {
    const query: Record<string, unknown> = { ...route.query, tab: value === "documents" ? undefined : value };
    if (value !== "documents") delete query.doc;
    void router.replace({ query: query as Record<string, string | undefined> });
  },
});

const tabs = computed(() => {
  const items = [
    { value: "documents" as Tab, label: t("knowledge.tabs.documents"), count: stats.value?.total ?? kb.value?.doc_count ?? null, testId: "tab-documents" },
    { value: "search" as Tab, label: t("knowledge.tabs.search"), testId: "tab-search" },
    { value: "versions" as Tab, label: t("knowledge.tabs.versions"), testId: "tab-versions" },
  ];
  if (permissions.can("kb:manage", id.value)) {
    items.push({ value: "settings" as Tab, label: t("knowledge.tabs.settings"), testId: "tab-settings" });
  }
  return items;
});

const building = computed(() => !!kb.value?.building_index_version);
const activeWork = computed(
  () => building.value || kb.value?.status === "indexing" || documents.value.some((doc) => !isTerminal(doc.state)),
);
const modelLabel = computed(() => {
  const model = options.value?.models.find((item) => item.id === kb.value?.embedding_model_id);
  return model ? `${model.display_name} · ${model.dimension}d` : kb.value ? `${kb.value.embedding_dim}d` : "";
});

async function refresh(preserveLoaded = false): Promise<void> {
  const requestedId = id.value;
  const signal = controller.signal;
  const visibleCount = preserveLoaded ? documents.value.length : 0;
  try {
    const [current, page] = await Promise.all([
      knowledge.get(requestedId, signal),
      knowledge.documents(requestedId, signal),
    ]);
    while (page.next_cursor && page.items.length < visibleCount) {
      const next = await knowledge.documents(requestedId, signal, page.next_cursor);
      page.items.push(...next.items);
      page.next_cursor = next.next_cursor;
    }
    if (disposed || signal.aborted || requestedId !== id.value) return;
    kb.value = current;
    documents.value = page.items;
    cursor.value = page.next_cursor;
    accessBlocked.value = false;
    error.value = null;
    void loadCounts(requestedId, signal);
  } catch (caught) {
    if (disposed || isAbort(caught)) return;
    if (caught instanceof ApiError && caught.code === "CONTENT_ACCESS_REQUIRES_GRANT") {
      accessBlocked.value = true;
      // The base itself may still be readable for its metadata.
      try {
        kb.value = await knowledge.get(requestedId, signal);
      } catch {
        // Nothing more to show.
      }
      return;
    }
    const failure = describeError(caught);
    error.value = failure.requestId ? { message: failure.message, requestId: failure.requestId } : { message: failure.message };
  }
}

async function loadCounts(requestedId: string, signal: AbortSignal): Promise<void> {
  try {
    const [counts, indexProgress] = await Promise.all([
      knowledge.stats(requestedId, signal),
      building.value || kb.value?.status === "indexing" ? knowledge.indexProgress(requestedId, signal) : Promise.resolve(null),
    ]);
    if (requestedId !== id.value) return;
    stats.value = counts;
    progress.value = indexProgress;
  } catch {
    // Counts are decoration on top of the list; a failure here is not fatal.
  }
}

function schedule(): void {
  if (disposed) return;
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => void poll(), activeWork.value ? ACTIVE_POLL_MS : IDLE_POLL_MS);
}

async function poll(): Promise<void> {
  await refresh(true);
  schedule();
}

async function loadMore(): Promise<void> {
  if (!cursor.value) return;
  try {
    const page = await knowledge.documents(id.value, controller.signal, cursor.value);
    documents.value.push(...page.items);
    cursor.value = page.next_cursor;
  } catch (caught) {
    if (!isAbort(caught)) toasts.error(describeError(caught).message);
  }
}

async function rebuild(): Promise<void> {
  try {
    const estimate = await knowledge.reindexEstimate(id.value);
    const accepted = await confirm({
      title: t("knowledge.rebuild.title"),
      description: t("knowledge.rebuild.description", {
        chunks: formatNumber(estimate.chunks, locale.value),
        minutes: estimate.estimated_minutes,
      }),
      confirmLabel: t("knowledge.rebuild.confirm"),
    });
    if (!accepted) return;
    await knowledge.reindex(id.value);
    toasts.success(t("knowledge.rebuild.started"));
    await refresh(true);
    schedule();
  } catch (caught) {
    toasts.error(t("knowledge.rebuild.failed"), describeError(caught).message);
  }
}

async function remove(): Promise<void> {
  if (!kb.value) return;
  const accepted = await confirm({
    title: t("knowledge.delete.title", { name: kb.value.name }),
    description: t("knowledge.delete.description"),
    confirmLabel: t("common.delete"),
    danger: true,
  });
  if (!accepted) return;
  try {
    await knowledge.remove(id.value);
    toasts.success(t("knowledge.delete.done", { name: kb.value.name }));
    await router.replace({ name: "knowledge" });
  } catch (caught) {
    toasts.error(t("knowledge.delete.failed"), describeError(caught).message);
  }
}

function onGranted(): void {
  breakGlassOpen.value = false;
  accessBlocked.value = false;
  void refresh();
}

async function initialise(): Promise<void> {
  loading.value = true;
  await refresh();
  loading.value = false;
  if (!options.value) {
    try {
      options.value = await knowledge.options(controller.signal);
    } catch {
      // Options only decorate the settings tab and the model label.
    }
  }
  schedule();
}

onMounted(initialise);

onBeforeUnmount(() => {
  disposed = true;
  controller.abort();
  if (timer) clearTimeout(timer);
});

watch(id, async () => {
  controller.abort();
  controller = new AbortController();
  if (timer) clearTimeout(timer);
  kb.value = undefined;
  documents.value = [];
  cursor.value = null;
  stats.value = null;
  progress.value = null;
  error.value = null;
  accessBlocked.value = false;
  await initialise();
});
</script>

<template>
  <div>
    <PageHeader
      :title="kb?.name ?? t('nav.knowledge')"
      :back="{ name: 'knowledge' }"
      :back-label="t('nav.knowledge')"
      :description="kb?.description ?? undefined"
    >
      <template #badges>
        <Badge v-if="kb" :tone="kbTone(kb.status)" dot :pulse="kb.status === 'indexing'" data-test="kb-status">
          {{ t(`knowledge.status.${kb.status}`) }}
        </Badge>
        <Badge v-if="kb?.reindex_required" tone="warn">{{ t("knowledge.rebuildRequired") }}</Badge>
      </template>
      <template #actions>
        <Button variant="outline" size="md" data-test="go-search" @click="tab = 'search'">
          <Search aria-hidden="true" />
          {{ t("knowledge.tabs.search") }}
        </Button>
        <Button
          v-if="permissions.can('kb:write', id) && tab === 'documents'"
          variant="primary"
          data-test="upload-button"
          @click="documentsPanel?.openFilePicker()"
        >
          <Upload aria-hidden="true" />
          {{ t("knowledge.documents.upload") }}
        </Button>
        <DropdownMenu>
          <template #trigger>
            <Button variant="outline" size="icon" :aria-label="t('common.actions')" data-test="kb-actions">
              <MoreHorizontal aria-hidden="true" />
            </Button>
          </template>
          <DropdownItem @select="refresh()">
            <RefreshCw aria-hidden="true" />
            {{ t("common.refresh") }}
          </DropdownItem>
          <template v-if="permissions.can('kb:manage', id)">
            <DropdownItem :disabled="building" data-test="rebuild" @select="rebuild">{{ t("knowledge.rebuild.action") }}</DropdownItem>
            <DropdownItem kind="separator" />
            <DropdownItem danger data-test="delete-kb" @select="remove">{{ t("knowledge.delete.action") }}</DropdownItem>
          </template>
        </DropdownMenu>
      </template>
      <template #meta>
        <dl v-if="kb" class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12.5px] text-ink-2" data-test="kb-facts">
          <div><dt class="sr-only">{{ t("knowledge.facts.activeIndex") }}</dt><dd class="tnum">{{ t("knowledge.facts.activeIndex") }} <strong class="text-ink">{{ kb.active_index_version ? `v${kb.active_index_version}` : "—" }}</strong></dd></div>
          <div><dt class="sr-only">{{ t("knowledge.columns.documents") }}</dt><dd class="tnum">{{ t("knowledge.columns.documents") }} <strong class="text-ink">{{ formatNumber(kb.doc_count, locale) }}</strong></dd></div>
          <div><dt class="sr-only">{{ t("knowledge.columns.chunks") }}</dt><dd class="tnum">{{ t("knowledge.columns.chunks") }} <strong class="text-ink">{{ formatNumber(kb.chunk_count, locale) }}</strong></dd></div>
          <div><dt class="sr-only">{{ t("knowledge.columns.size") }}</dt><dd class="tnum">{{ formatBytes(kb.bytes_used) }}</dd></div>
          <div v-if="modelLabel"><dt class="sr-only">{{ t("knowledge.create.model") }}</dt><dd>{{ modelLabel }}</dd></div>
          <div><dt class="sr-only">{{ t("knowledge.columns.lastIndexed") }}</dt><dd>{{ t("knowledge.columns.lastIndexed") }} {{ formatRelative(kb.last_indexed_at, locale) }}</dd></div>
        </dl>
      </template>
    </PageHeader>

    <ErrorState v-if="error" :message="error.message" :request-id="error.requestId" class="mb-4" @retry="refresh()" />

    <Notice v-if="accessBlocked" tone="warn" :title="t('knowledge.access.blockedTitle')" class="mb-4" test-id="access-blocked">
      {{ t("knowledge.access.blockedBody") }}
      <div class="mt-2">
        <Button size="sm" variant="secondary" data-test="request-access" @click="breakGlassOpen = true">{{ t("knowledge.access.request") }}</Button>
      </div>
    </Notice>

    <div
      v-if="kb && building"
      class="mb-4 rounded-md border border-info/25 bg-info-soft px-3 py-2.5"
      data-test="build-progress"
    >
      <div class="flex flex-wrap items-center justify-between gap-2 text-[12.5px]">
        <span class="font-medium text-ink">
          {{ t("knowledge.build.inProgress", { v: kb.building_index_version }) }}
        </span>
        <span class="tnum text-ink-2">
          <template v-if="progress">
            {{ formatNumber(progress.chunk_done, locale) }} / {{ formatNumber(progress.chunk_total, locale) }}
            <span v-if="progress.eta_seconds !== null"> · {{ t("knowledge.build.eta", { time: formatDuration(progress.eta_seconds) }) }}</span>
          </template>
        </span>
      </div>
      <ProgressBar :value="progress?.percent ?? 0" :indeterminate="!progress" class="mt-2" tone="brand" />
    </div>

    <Skeleton v-if="loading" :rows="6" class="mt-2" />

    <template v-else-if="kb">
      <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
        <SegmentedControl v-model="tab" :options="tabs" :aria-label="t('knowledge.tabs.label')" />
      </div>

      <DocumentsPanel
        v-if="tab === 'documents'"
        ref="documentsPanel"
        :kb="kb"
        :documents="documents"
        :has-more="!!cursor"
        :options="options"
        :can-write="permissions.can('kb:write', id)"
        @refresh="refresh(true)"
        @load-more="loadMore"
      />
      <SearchPanel v-else-if="tab === 'search'" :kb="kb" :documents="documents" />
      <VersionsPanel v-else-if="tab === 'versions'" :kb="kb" :progress="progress" :can-manage="permissions.can('kb:manage', id)" @rebuild="rebuild" />
      <KbSettingsPanel v-else-if="tab === 'settings'" :kb="kb" :options="options" @saved="refresh(true)" @delete="remove" />
    </template>

    <BreakGlassDialog v-model:open="breakGlassOpen" :kb-id="id" @granted="onGranted" />
  </div>
</template>
