<script setup lang="ts">
/**
 * Knowledge base list. One row per base with the facts that answer "is it
 * healthy and is it current"; everything else lives in the detail page.
 */
import { BookOpen, MoreHorizontal, Plus, Search } from "lucide-vue-next";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";

import { knowledge, type KnowledgeBase } from "@/api/knowledge";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import DropdownItem from "@/components/ui/DropdownItem.vue";
import DropdownMenu from "@/components/ui/DropdownMenu.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import Input from "@/components/ui/Input.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import CreateKnowledgeBaseSheet from "@/features/knowledge/CreateKnowledgeBaseSheet.vue";
import { formatBytes, formatNumber, formatRelative } from "@/lib/utils";
import PageHeader from "@/shared/components/PageHeader.vue";
import { confirm } from "@/shared/composables/useConfirm";
import { useKnowledgePermissions } from "@/shared/composables/useKnowledgePermissions";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError, isAbort } from "@/shared/errors";
import { kbTone } from "@/shared/pipeline";

const { t, locale } = useI18n();
const router = useRouter();
const route = useRoute();
const permissions = useKnowledgePermissions();
const toasts = useToasts();

const rows = ref<KnowledgeBase[]>([]);
const cursor = ref<string | null>(null);
const loading = ref(true);
const loadingMore = ref(false);
const error = ref<{ message: string; requestId?: string } | null>(null);
const filter = ref("");
const createOpen = ref(false);
const controller = new AbortController();

const filtered = computed(() => {
  const needle = filter.value.trim().toLowerCase();
  if (!needle) return rows.value;
  return rows.value.filter(
    (kb) => kb.name.toLowerCase().includes(needle) || (kb.description ?? "").toLowerCase().includes(needle),
  );
});

async function load(more = false): Promise<void> {
  if (more) loadingMore.value = true;
  else loading.value = true;
  error.value = null;
  try {
    const page = await knowledge.list(more ? (cursor.value ?? undefined) : undefined, controller.signal);
    rows.value = more ? [...rows.value, ...page.items] : page.items;
    cursor.value = page.next_cursor;
  } catch (caught) {
    if (!isAbort(caught)) {
      const failure = describeError(caught);
      error.value = failure.requestId ? { message: failure.message, requestId: failure.requestId } : { message: failure.message };
    }
  } finally {
    loading.value = false;
    loadingMore.value = false;
  }
}

async function remove(kb: KnowledgeBase): Promise<void> {
  const accepted = await confirm({
    title: t("knowledge.delete.title", { name: kb.name }),
    description: t("knowledge.delete.description"),
    confirmLabel: t("common.delete"),
    danger: true,
  });
  if (!accepted) return;
  try {
    await knowledge.remove(kb.id);
    toasts.success(t("knowledge.delete.done", { name: kb.name }));
    await load();
  } catch (caught) {
    toasts.error(t("knowledge.delete.failed"), describeError(caught).message);
  }
}

async function onCreated(kb: KnowledgeBase): Promise<void> {
  createOpen.value = false;
  toasts.success(t("knowledge.create.done", { name: kb.name }));
  await router.push({ name: "knowledge-detail", params: { id: kb.id } });
}

watch(
  () => route.query.create,
  (value) => {
    if (value === "1") {
      createOpen.value = true;
      void router.replace({ name: "knowledge", query: {} });
    }
  },
  { immediate: true },
);

onMounted(() => void load());
onBeforeUnmount(() => controller.abort());
</script>

<template>
  <div>
    <PageHeader :title="t('nav.knowledge')" :description="t('knowledge.lede')">
      <template #actions>
        <Input v-model="filter" class="w-56" :placeholder="t('knowledge.filter')" data-test="kb-filter" type="search">
          <template #prefix><Search /></template>
        </Input>
        <Button variant="primary" :disabled="!permissions.can('kb:create')" data-test="create-kb" @click="createOpen = true">
          <Plus aria-hidden="true" />
          {{ t("knowledge.new") }}
        </Button>
      </template>
    </PageHeader>

    <ErrorState v-if="error" :message="error.message" :request-id="error.requestId" class="mb-4" @retry="load()" />

    <Skeleton v-if="loading" :rows="5" />

    <EmptyState
      v-else-if="!rows.length && !error"
      :title="t('knowledge.empty.title')"
      :description="t('knowledge.empty.description')"
    >
      <template #icon><BookOpen /></template>
      <Button v-if="permissions.can('kb:create')" variant="primary" @click="createOpen = true">{{ t("knowledge.new") }}</Button>
    </EmptyState>

    <EmptyState v-else-if="!filtered.length && filter.trim()" :title="t('knowledge.noMatch', { q: filter.trim() })" compact />

    <div v-else class="overflow-hidden rounded-md border border-line" data-test="kb-table">
      <div class="hidden grid-cols-[minmax(0,2fr)_110px_90px_90px_80px_130px_40px] gap-3 border-b border-line bg-surface-2 px-3 py-1.5 text-[11px] font-medium uppercase tracking-wide text-ink-3 md:grid">
        <span>{{ t("knowledge.columns.name") }}</span>
        <span>{{ t("knowledge.columns.status") }}</span>
        <span class="text-right">{{ t("knowledge.columns.documents") }}</span>
        <span class="text-right">{{ t("knowledge.columns.chunks") }}</span>
        <span class="text-right">{{ t("knowledge.columns.size") }}</span>
        <span>{{ t("knowledge.columns.lastIndexed") }}</span>
        <span />
      </div>
      <ul class="divide-y divide-line">
        <li
          v-for="kb in filtered"
          :key="kb.id"
          class="group grid grid-cols-[minmax(0,1fr)_40px] items-center gap-3 px-3 py-2.5 transition-colors hover:bg-surface-2 md:grid-cols-[minmax(0,2fr)_110px_90px_90px_80px_130px_40px]"
          :data-test="`kb-row-${kb.id}`"
        >
          <RouterLink :to="{ name: 'knowledge-detail', params: { id: kb.id } }" class="min-w-0 rounded-xs outline-none focus-visible:ring-2 focus-visible:ring-brand/40">
            <span class="block truncate text-[13.5px] font-medium text-ink group-hover:text-brand-strong">{{ kb.name }}</span>
            <span class="block truncate text-[12px] text-ink-3">{{ kb.description || `v${kb.active_index_version ?? "—"} · ${kb.embedding_dim}d · ${kb.metric}` }}</span>
            <span class="mt-1 flex items-center gap-2 md:hidden">
              <Badge :tone="kbTone(kb.status)" size="sm" dot>{{ t(`knowledge.status.${kb.status}`) }}</Badge>
              <span class="tnum text-[12px] text-ink-3">{{ t("knowledge.facts.documents", { n: formatNumber(kb.doc_count, locale) }) }}</span>
            </span>
          </RouterLink>
          <span class="hidden md:block">
            <Badge :tone="kbTone(kb.status)" size="sm" dot :pulse="kb.status === 'indexing'">{{ t(`knowledge.status.${kb.status}`) }}</Badge>
            <Badge v-if="kb.reindex_required" tone="warn" size="sm" class="ml-1">{{ t("knowledge.rebuildRequired") }}</Badge>
          </span>
          <span class="tnum hidden text-right text-[13px] text-ink md:block">{{ formatNumber(kb.doc_count, locale) }}</span>
          <span class="tnum hidden text-right text-[13px] text-ink-2 md:block">{{ formatNumber(kb.chunk_count, locale) }}</span>
          <span class="tnum hidden text-right text-[12.5px] text-ink-2 md:block">{{ formatBytes(kb.bytes_used) }}</span>
          <span class="tnum hidden text-[12.5px] text-ink-2 md:block">{{ formatRelative(kb.last_indexed_at, locale) }}</span>
          <DropdownMenu>
            <template #trigger>
              <Button variant="ghost" size="icon-sm" :aria-label="t('common.actions')" data-test="kb-menu">
                <MoreHorizontal aria-hidden="true" />
              </Button>
            </template>
            <DropdownItem @select="router.push({ name: 'knowledge-detail', params: { id: kb.id } })">{{ t("common.open") }}</DropdownItem>
            <DropdownItem @select="router.push({ name: 'knowledge-detail', params: { id: kb.id }, query: { tab: 'search' } })">{{ t("knowledge.tabs.search") }}</DropdownItem>
            <template v-if="permissions.can('kb:manage', kb.id)">
              <DropdownItem kind="separator" />
              <DropdownItem danger data-test="kb-delete" @select="remove(kb)">{{ t("common.delete") }}</DropdownItem>
            </template>
          </DropdownMenu>
        </li>
      </ul>
      <div v-if="cursor" class="border-t border-line px-3 py-2 text-center">
        <Button variant="ghost" size="sm" :loading="loadingMore" @click="load(true)">{{ t("common.loadMore") }}</Button>
      </div>
    </div>

    <CreateKnowledgeBaseSheet v-model:open="createOpen" @created="onCreated" />
  </div>
</template>
