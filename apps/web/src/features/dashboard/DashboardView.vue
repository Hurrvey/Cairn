<script setup lang="ts">
/**
 * Overview: what needs attention and where to go next.
 *
 * Built from the same endpoints the detail pages use, so nothing here can
 * disagree with what a user finds one click deeper. Per-knowledge-base
 * document counts are fetched for the first few bases only; the list itself
 * is complete.
 */
import { AlertTriangle, ArrowRight, BookOpen, Cable, Check, CircleDashed, KeyRound, Plus } from "lucide-vue-next";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";

import { queues, type QueueStat } from "@/api/admin";
import { knowledge, type DocumentStateCounts, type KnowledgeBase, type SetupOptions } from "@/api/knowledge";
import { mcpService, type MCPServiceSnapshot } from "@/api/mcp";
import { meta } from "@/api/meta";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import CopyButton from "@/components/ui/CopyButton.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import { formatNumber, formatRelative } from "@/lib/utils";
import Section from "@/shared/components/Section.vue";
import { useKnowledgePermissions } from "@/shared/composables/useKnowledgePermissions";
import { usePermissions } from "@/shared/composables/usePermissions";
import { describeError, isAbort } from "@/shared/errors";
import { kbTone } from "@/shared/pipeline";
import { useSessionStore } from "@/stores/session";

const { t, locale } = useI18n();
const session = useSessionStore();
const permissions = usePermissions();
const kbPermissions = useKnowledgePermissions();

const bases = ref<KnowledgeBase[]>([]);
const stats = ref<Record<string, DocumentStateCounts>>({});
const options = ref<SetupOptions | null>(null);
const mcp = ref<MCPServiceSnapshot | null>(null);
const queueStats = ref<QueueStat[]>([]);
const capabilities = ref<Record<string, boolean>>({});
const loading = ref(true);
const error = ref<{ message: string; requestId?: string } | null>(null);
const controller = new AbortController();

const STATS_LIMIT = 8;

const greeting = computed(() => {
  const hour = new Date().getHours();
  const key = hour < 5 ? "night" : hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
  return t(`dashboard.greeting.${key}`, { name: session.user?.display_name || session.user?.username || "" });
});

const setup = computed(() => {
  if (!permissions.isAdmin.value || !options.value) return null;
  const objectBinding = options.value.bindings.some((binding) => binding.kind === "object");
  const vectorBinding = options.value.bindings.some((binding) => binding.kind === "vector");
  const steps = [
    { key: "model", done: options.value.models.length > 0, route: "models" },
    { key: "storage", done: objectBinding && vectorBinding, route: "storage" },
    { key: "knowledge", done: bases.value.length > 0, route: "knowledge" },
  ];
  return steps.every((step) => step.done) ? null : steps;
});

interface Attention {
  key: string;
  tone: "bad" | "warn" | "info";
  text: string;
  route: { name: string; params?: Record<string, string>; query?: Record<string, string> };
}

const attention = computed<Attention[]>(() => {
  const items: Attention[] = [];
  for (const kb of bases.value) {
    const counts = stats.value[kb.id]?.counts ?? {};
    const failed = counts.failed ?? 0;
    if (failed > 0) {
      items.push({
        key: `failed:${kb.id}`,
        tone: "bad",
        text: t("dashboard.attention.failedDocuments", { n: failed, kb: kb.name }),
        route: { name: "knowledge-detail", params: { id: kb.id }, query: { tab: "documents", state: "failed" } },
      });
    }
    if (kb.building_index_version) {
      items.push({
        key: `building:${kb.id}`,
        tone: "info",
        text: t("dashboard.attention.building", { kb: kb.name, v: kb.building_index_version }),
        route: { name: "knowledge-detail", params: { id: kb.id }, query: { tab: "versions" } },
      });
    }
    if (kb.reindex_required) {
      items.push({
        key: `reindex:${kb.id}`,
        tone: "warn",
        text: t("dashboard.attention.reindexRequired", { kb: kb.name }),
        route: { name: "knowledge-detail", params: { id: kb.id }, query: { tab: "versions" } },
      });
    }
    if (kb.status === "error") {
      items.push({
        key: `error:${kb.id}`,
        tone: "bad",
        text: t("dashboard.attention.kbError", { kb: kb.name }),
        route: { name: "knowledge-detail", params: { id: kb.id } },
      });
    }
  }
  if (mcp.value?.managed && mcp.value.state === "error") {
    items.push({ key: "mcp", tone: "bad", text: t("dashboard.attention.mcpError"), route: { name: "mcp-service" } });
  }
  for (const queue of queueStats.value) {
    if (queue.oldest_ready_age_seconds > 600) {
      items.push({
        key: `queue:${queue.queue}`,
        tone: "warn",
        text: t("dashboard.attention.queueStalled", { queue: queue.queue, minutes: Math.round(queue.oldest_ready_age_seconds / 60) }),
        route: { name: "settings" },
      });
    }
  }
  return items;
});

const processing = (kb: KnowledgeBase): number => {
  const counts = stats.value[kb.id]?.counts;
  if (!counts) return 0;
  return Object.entries(counts)
    .filter(([state]) => !["indexed", "failed", "skipped"].includes(state))
    .reduce((sum, [, n]) => sum + n, 0);
};
const failed = (kb: KnowledgeBase): number => stats.value[kb.id]?.counts?.failed ?? 0;

const retrievalEndpoint = `${window.location.origin}/v1/retrieval/query`;
const mcpEndpoint = `${window.location.origin}/mcp`;

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  const signal = controller.signal;
  try {
    const [page, capability] = await Promise.all([
      knowledge.list(undefined, signal),
      meta().catch(() => ({ capabilities: {} as Record<string, boolean> })),
    ]);
    bases.value = page.items;
    capabilities.value = capability.capabilities;
    const extras: Promise<unknown>[] = [
      ...page.items.slice(0, STATS_LIMIT).map(async (kb) => {
        try {
          stats.value = { ...stats.value, [kb.id]: await knowledge.stats(kb.id, signal) };
        } catch {
          // A base the user cannot read simply shows no counts.
        }
      }),
    ];
    if (permissions.isAdmin.value) {
      extras.push(knowledge.options(signal).then((value) => (options.value = value)).catch(() => undefined));
    }
    if (permissions.canManageSettings.value) {
      extras.push(mcpService.get(signal).then((value) => (mcp.value = value)).catch(() => undefined));
      extras.push(queues.stats().then((value) => (queueStats.value = value.queues)).catch(() => undefined));
    }
    await Promise.all(extras);
  } catch (caught) {
    if (!isAbort(caught)) {
      const failure = describeError(caught);
      error.value = failure.requestId ? { message: failure.message, requestId: failure.requestId } : { message: failure.message };
    }
  } finally {
    loading.value = false;
  }
}

onMounted(load);
onBeforeUnmount(() => controller.abort());
</script>

<template>
  <div>
    <header class="mb-6">
      <h1 class="text-[24px] font-semibold text-ink">{{ greeting }}</h1>
      <p class="mt-1 text-[13px] text-ink-2">{{ t("dashboard.lede") }}</p>
    </header>

    <ErrorState v-if="error" :message="error.message" :request-id="error.requestId" class="mb-5" @retry="load" />

    <Section v-if="setup" :title="t('dashboard.setup.title')" :description="t('dashboard.setup.description')" id="setup">
      <ol class="grid gap-2 sm:grid-cols-3" data-test="setup-checklist">
        <li v-for="(step, index) in setup" :key="step.key">
          <RouterLink
            :to="{ name: step.route }"
            :class="[
              'flex h-full items-start gap-3 rounded-md border px-3 py-3 transition-colors',
              step.done ? 'border-line bg-surface-2 text-ink-3' : 'border-brand/40 bg-surface hover:border-brand',
            ]"
            :data-test="`setup-${step.key}`"
          >
            <span
              :class="[
                'flex size-6 shrink-0 items-center justify-center rounded-full text-[12px] font-semibold',
                step.done ? 'bg-ok-soft text-ok' : 'bg-brand text-brand-ink',
              ]"
            >
              <Check v-if="step.done" class="size-3.5" aria-hidden="true" />
              <span v-else>{{ index + 1 }}</span>
            </span>
            <span class="min-w-0">
              <span :class="['block text-[13px] font-medium', step.done ? 'line-through' : 'text-ink']">{{ t(`dashboard.setup.${step.key}`) }}</span>
              <span class="mt-0.5 block text-[12px] leading-snug text-ink-2">{{ t(`dashboard.setup.${step.key}Hint`) }}</span>
            </span>
          </RouterLink>
        </li>
      </ol>
    </Section>

    <Section :title="t('dashboard.attention.title')" id="attention">
      <Skeleton v-if="loading" :rows="2" />
      <ul v-else-if="attention.length" class="divide-y divide-line rounded-md border border-line" data-test="attention-list">
        <li v-for="item in attention" :key="item.key">
          <RouterLink :to="item.route" class="flex items-center gap-3 px-3 py-2.5 text-[13px] text-ink transition-colors hover:bg-surface-2">
            <AlertTriangle v-if="item.tone !== 'info'" :class="['size-4 shrink-0', item.tone === 'bad' ? 'text-bad' : 'text-warn']" aria-hidden="true" />
            <CircleDashed v-else class="size-4 shrink-0 animate-[spin_3s_linear_infinite] text-info" aria-hidden="true" />
            <span class="min-w-0 flex-1 truncate">{{ item.text }}</span>
            <ArrowRight class="size-4 shrink-0 text-ink-3" aria-hidden="true" />
          </RouterLink>
        </li>
      </ul>
      <p v-else class="flex items-center gap-2 text-[13px] text-ink-2" data-test="all-clear">
        <Check class="size-4 text-ok" aria-hidden="true" />
        {{ t("dashboard.attention.clear") }}
      </p>
    </Section>

    <Section :title="t('dashboard.bases.title')" id="bases">
      <template #actions>
        <Button v-if="kbPermissions.can('kb:create')" variant="primary" size="sm" :to="{ name: 'knowledge', query: { create: '1' } }" data-test="new-kb">
          <Plus aria-hidden="true" />
          {{ t("knowledge.new") }}
        </Button>
        <Button variant="outline" size="sm" :to="{ name: 'knowledge' }">{{ t("dashboard.bases.all") }}</Button>
      </template>
      <Skeleton v-if="loading" :rows="3" />
      <EmptyState
        v-else-if="!bases.length && !error"
        :title="t('knowledge.empty.title')"
        :description="setup ? t('knowledge.empty.setupFirst') : t('knowledge.empty.description')"
        compact
      >
        <template #icon><BookOpen /></template>
        <Button v-if="kbPermissions.can('kb:create') && !setup" variant="primary" size="sm" :to="{ name: 'knowledge', query: { create: '1' } }">
          {{ t("knowledge.new") }}
        </Button>
      </EmptyState>
      <ul v-else class="divide-y divide-line rounded-md border border-line" data-test="kb-list">
        <li v-for="kb in bases" :key="kb.id">
          <RouterLink
            :to="{ name: 'knowledge-detail', params: { id: kb.id } }"
            class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 px-3 py-2.5 transition-colors hover:bg-surface-2 sm:grid-cols-[minmax(0,1fr)_auto_auto_auto]"
            :data-test="`kb-row-${kb.id}`"
          >
            <span class="min-w-0">
              <span class="flex items-center gap-2">
                <span class="truncate text-[13.5px] font-medium text-ink">{{ kb.name }}</span>
                <Badge :tone="kbTone(kb.status)" size="sm" dot :pulse="kb.status === 'indexing'">{{ t(`knowledge.status.${kb.status}`) }}</Badge>
              </span>
              <span v-if="kb.description" class="mt-0.5 block truncate text-[12px] text-ink-3">{{ kb.description }}</span>
            </span>
            <span class="tnum hidden text-[12.5px] text-ink-2 sm:block">
              {{ t("knowledge.facts.documents", { n: formatNumber(kb.doc_count, locale) }) }}
              <span v-if="processing(kb)" class="ml-1 text-info">· {{ t("knowledge.facts.processing", { n: processing(kb) }) }}</span>
              <span v-if="failed(kb)" class="ml-1 text-bad">· {{ t("knowledge.facts.failed", { n: failed(kb) }) }}</span>
            </span>
            <span class="tnum hidden text-[12px] text-ink-3 sm:block">{{ kb.active_index_version ? `v${kb.active_index_version}` : "—" }}</span>
            <span class="tnum text-[12px] text-ink-3">{{ formatRelative(kb.last_indexed_at, locale) }}</span>
          </RouterLink>
        </li>
      </ul>
    </Section>

    <Section :title="t('dashboard.connect.title')" :description="t('dashboard.connect.description')" id="connect">
      <div class="grid gap-2 md:grid-cols-2">
        <div class="flex items-center gap-3 rounded-md border border-line bg-surface px-3 py-2.5">
          <KeyRound class="size-4 shrink-0 text-ink-3" aria-hidden="true" />
          <div class="min-w-0 flex-1">
            <p class="text-[12px] text-ink-3">{{ t("dashboard.connect.retrieval") }}</p>
            <code class="block truncate text-[12.5px] text-ink" data-test="retrieval-endpoint">{{ retrievalEndpoint }}</code>
          </div>
          <CopyButton :value="retrievalEndpoint" />
        </div>
        <div v-if="capabilities.mcp" class="flex items-center gap-3 rounded-md border border-line bg-surface px-3 py-2.5">
          <Cable class="size-4 shrink-0 text-ink-3" aria-hidden="true" />
          <div class="min-w-0 flex-1">
            <p class="text-[12px] text-ink-3">{{ t("dashboard.connect.mcp") }}</p>
            <code class="block truncate text-[12.5px] text-ink" data-test="mcp-endpoint">{{ mcpEndpoint }}</code>
          </div>
          <CopyButton :value="mcpEndpoint" />
        </div>
      </div>
      <p class="mt-3 text-[12.5px] text-ink-2">
        {{ t("dashboard.connect.keysHint") }}
        <RouterLink :to="{ name: 'api-keys' }" class="text-brand underline-offset-2 hover:underline">{{ t("nav.apiKeys") }}</RouterLink>
      </p>
    </Section>
  </div>
</template>
