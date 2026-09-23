<script setup lang="ts">
/**
 * Audit log viewer (FR-O-02).
 *
 * Cursor pagination, not offset: the log is append-only and written constantly,
 * so an offset page would skip or repeat rows between clicks.
 *
 * `before`/`after` are rendered as raw JSON on demand. Prettifying them would
 * mean interpreting every resource shape, and during a security review the
 * exact recorded value is the thing being examined.
 */
import { ChevronDown, Download, ScrollText } from "lucide-vue-next";
import { computed, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";

import { type AuditEntry, audit } from "@/api/admin";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import Input from "@/components/ui/Input.vue";
import NativeSelect from "@/components/ui/NativeSelect.vue";
import SegmentedControl from "@/components/ui/SegmentedControl.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import { cn, formatDateTime } from "@/lib/utils";
import PageHeader from "@/shared/components/PageHeader.vue";
import { describeError } from "@/shared/errors";
import type { Tone } from "@/components/ui/types";

const { t, locale } = useI18n();

const filters = reactive({ quick: "all", action_prefix: "", outcome: "", actor_id: "", since: "" });
const entries = ref<AuditEntry[]>([]);
const cursor = ref<string | null>(null);
const hasMore = ref(false);
const loading = ref(false);
const error = ref<{ message: string; requestId?: string } | null>(null);
const expanded = ref<number | null>(null);

/** Prefixes worth one click. `authz.` and `break_glass.` are the two an auditor
 *  reaches for first, so they get a shortcut rather than a typed filter. */
const QUICK: Record<string, string> = {
  all: "",
  auth: "auth.",
  user: "user.",
  grant: "grant.",
  breakGlass: "break_glass.",
  apikey: "apikey.",
  denied: "authz.denied",
};
const quickOptions = computed(() => Object.keys(QUICK).map((key) => ({ value: key, label: t(`audit.quick.${key}`) })));
const exportHref = computed(() => audit.exportUrl(filters.since ? { since: new Date(filters.since).toISOString() } : {}));

function outcomeTone(outcome: string): Tone {
  if (outcome === "success") return "ok";
  return outcome === "denied" ? "warn" : "bad";
}

async function load(reset = true): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const page = await audit.query({
      action_prefix: filters.action_prefix || QUICK[filters.quick] || undefined,
      outcome: filters.outcome || undefined,
      actor_id: filters.actor_id || undefined,
      since: filters.since ? new Date(filters.since).toISOString() : undefined,
      limit: 50,
      cursor: reset ? undefined : (cursor.value ?? undefined),
    });
    entries.value = reset ? page.items : [...entries.value, ...page.items];
    cursor.value = page.next_cursor;
    hasMore.value = page.has_more;
  } catch (caught) {
    const failure = describeError(caught);
    error.value = failure.requestId ? { message: failure.message, requestId: failure.requestId } : { message: failure.message };
  } finally {
    loading.value = false;
  }
}

function applyQuick(value: string): void {
  filters.quick = value;
  filters.action_prefix = "";
  void load();
}

function detailOf(entry: AuditEntry): string {
  return JSON.stringify({ before: entry.before, after: entry.after, detail: entry.detail }, null, 2);
}

void load();
</script>

<template>
  <div>
    <PageHeader :title="t('audit.title')" :description="t('audit.subtitle')">
      <template #actions>
        <Button variant="secondary" :href="exportHref" download data-test="export">
          <Download aria-hidden="true" />
          {{ t("audit.export") }}
        </Button>
      </template>
    </PageHeader>

    <div class="mb-3 flex flex-wrap items-center gap-2" data-test="quick-filters">
      <SegmentedControl :model-value="filters.quick" :options="quickOptions" size="sm" :aria-label="t('audit.quick.label')" @update:model-value="applyQuick" />
    </div>
    <form class="mb-4 grid gap-2 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1.5fr)_minmax(0,1.5fr)]" @submit.prevent="load()">
      <Input v-model="filters.action_prefix" size="sm" :placeholder="t('audit.actionPrefix')" data-test="filter-action" @change="load()" />
      <NativeSelect v-model="filters.outcome" size="sm" data-test="filter-outcome" @change="load()">
        <option value="">{{ t("audit.anyOutcome") }}</option>
        <option v-for="outcome in ['success', 'failure', 'denied']" :key="outcome" :value="outcome">{{ t(`audit.outcomes.${outcome}`) }}</option>
      </NativeSelect>
      <Input v-model="filters.actor_id" size="sm" :placeholder="t('audit.actorId')" data-test="filter-actor" @change="load()" />
      <Input v-model="filters.since" size="sm" type="datetime-local" :aria-label="t('audit.since')" @change="load()" />
    </form>

    <ErrorState v-if="error" :message="error.message" :request-id="error.requestId" class="mb-4" @retry="load()" />

    <Skeleton v-if="loading && !entries.length" :rows="6" />
    <EmptyState v-else-if="!entries.length && !loading && !error" :title="t('audit.empty')" compact>
      <template #icon><ScrollText /></template>
    </EmptyState>

    <div v-else class="overflow-hidden rounded-md border border-line" data-test="audit-table">
      <ul class="divide-y divide-line">
        <li v-for="entry in entries" :key="entry.id" :data-test="`audit-${entry.id}`">
          <div class="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-3 gap-y-1 px-3 py-2 sm:grid-cols-[150px_minmax(0,1fr)_minmax(0,1.3fr)_90px_auto] sm:items-center">
            <time :datetime="entry.at" class="tnum text-[12px] text-ink-3">{{ formatDateTime(entry.at, locale, "short") }}</time>
            <span class="min-w-0 sm:order-none">
              <span class="block truncate text-[13px] font-medium text-ink">{{ entry.actor_label }}</span>
              <span class="block text-[11.5px] text-ink-3">{{ entry.actor_type }}</span>
            </span>
            <span class="col-span-2 min-w-0 sm:col-span-1">
              <code class="block truncate text-[12.5px] text-ink">{{ entry.action }}</code>
              <span class="block truncate text-[11.5px] text-ink-3">{{ [entry.resource_type, entry.resource_id].filter(Boolean).join(" ") || "—" }}</span>
            </span>
            <Badge size="sm" :tone="outcomeTone(entry.outcome)" class="justify-self-start">{{ t(`audit.outcomes.${entry.outcome}`) }}</Badge>
            <Button
              variant="ghost"
              size="sm"
              class="justify-self-end"
              :aria-expanded="expanded === entry.id"
              data-test="toggle-detail"
              @click="expanded = expanded === entry.id ? null : entry.id"
            >
              {{ expanded === entry.id ? t("audit.hide") : t("audit.detail") }}
              <ChevronDown :class="cn('transition-transform', expanded === entry.id && 'rotate-180')" aria-hidden="true" />
            </Button>
          </div>
          <div v-if="expanded === entry.id" class="border-t border-line bg-surface-2 px-3 py-2.5">
            <p class="mb-1.5 flex flex-wrap gap-x-4 text-[11.5px] text-ink-3">
              <span v-if="entry.ip">IP {{ entry.ip }}</span>
              <span v-if="entry.request_id" class="font-mono">{{ entry.request_id }}</span>
            </p>
            <pre class="overflow-x-auto text-[12px] leading-relaxed text-ink" data-test="audit-detail">{{ detailOf(entry) }}</pre>
          </div>
        </li>
      </ul>
      <div class="border-t border-line px-3 py-2 text-center text-[12.5px] text-ink-3">
        <Button v-if="hasMore" variant="ghost" size="sm" :loading="loading" data-test="load-more" @click="load(false)">{{ t("common.loadMore") }}</Button>
        <span v-else>{{ t("audit.end") }}</span>
      </div>
    </div>
  </div>
</template>
