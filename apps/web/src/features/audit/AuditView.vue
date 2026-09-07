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
import { computed, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";

import { type AuditEntry, audit } from "@/api/admin";
import { ApiError } from "@/api/client";
import PageHeader from "@/shared/components/PageHeader.vue";

const { t } = useI18n();

const filters = reactive({
  action_prefix: "",
  outcome: "",
  actor_id: "",
  since: "",
});

const entries = ref<AuditEntry[]>([]);
const cursor = ref<string | null>(null);
const hasMore = ref(false);
const loading = ref(false);
const error = ref<string | null>(null);
const expanded = ref<number | null>(null);

const OUTCOMES = ["success", "failure", "denied"];

/** Prefixes worth one click. `authz.` and `break_glass.` are the two an auditor
 *  reaches for first, so they get a shortcut rather than a typed filter. */
const QUICK = [
  { key: "all", prefix: "" },
  { key: "auth", prefix: "auth." },
  { key: "user", prefix: "user." },
  { key: "grant", prefix: "grant." },
  { key: "breakGlass", prefix: "break_glass." },
  { key: "apikey", prefix: "apikey." },
  { key: "denied", prefix: "authz.denied" },
];

const exportHref = computed(() => audit.exportUrl(filters.since ? { since: filters.since } : {}));

async function load(reset = true): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const page = await audit.query({
      action_prefix: filters.action_prefix || undefined,
      outcome: filters.outcome || undefined,
      actor_id: filters.actor_id || undefined,
      since: filters.since || undefined,
      limit: 50,
      cursor: reset ? undefined : (cursor.value ?? undefined),
    });
    entries.value = reset ? page.items : [...entries.value, ...page.items];
    cursor.value = page.next_cursor;
    hasMore.value = page.has_more;
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught.detail : t("errors.unexpected");
  } finally {
    loading.value = false;
  }
}

function applyQuick(prefix: string): void {
  filters.action_prefix = prefix;
  void load();
}

function outcomeType(outcome: string): "success" | "danger" | "warning" {
  if (outcome === "success") return "success";
  return outcome === "denied" ? "warning" : "danger";
}

void load();
</script>

<template>
  <div>
    <PageHeader :title="t('audit.title')" :subtitle="t('audit.subtitle')">
      <template #actions>
        <el-button tag="a" :href="exportHref" download data-test="export">
          {{ t("audit.export") }}
        </el-button>
      </template>
    </PageHeader>

    <div class="quick" data-test="quick-filters">
      <el-button
        v-for="item in QUICK"
        :key="item.key"
        size="small"
        :type="filters.action_prefix === item.prefix ? 'primary' : 'default'"
        @click="applyQuick(item.prefix)"
      >
        {{ t(`audit.quick.${item.key}`) }}
      </el-button>
    </div>

    <div class="filters">
      <el-input
        v-model="filters.action_prefix"
        :placeholder="t('audit.actionPrefix')"
        clearable
        data-test="filter-action"
        @change="load()"
      />
      <el-select
        v-model="filters.outcome"
        :placeholder="t('audit.outcome')"
        clearable
        data-test="filter-outcome"
        @change="load()"
      >
        <el-option v-for="o in OUTCOMES" :key="o" :label="o" :value="o" />
      </el-select>
      <el-input
        v-model="filters.actor_id"
        :placeholder="t('audit.actorId')"
        clearable
        data-test="filter-actor"
        @change="load()"
      />
    </div>

    <el-alert v-if="error" type="error" :title="error" show-icon :closable="false" class="mb" />

    <el-table v-loading="loading" :data="entries" data-test="audit-table" row-key="id">
      <el-table-column :label="t('audit.when')" width="180">
        <template #default="{ row }">
          <span class="mono">{{ row.at }}</span>
        </template>
      </el-table-column>

      <el-table-column :label="t('audit.actor')" min-width="150">
        <template #default="{ row }">
          <strong>{{ row.actor_label }}</strong>
          <div class="muted small">{{ row.actor_type }}</div>
        </template>
      </el-table-column>

      <el-table-column :label="t('audit.action')" min-width="180">
        <template #default="{ row }">
          <span class="mono">{{ row.action }}</span>
          <div v-if="row.resource_type" class="muted small">
            {{ row.resource_type }}
          </div>
        </template>
      </el-table-column>

      <el-table-column :label="t('audit.outcome')" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="outcomeType(row.outcome)">{{ row.outcome }}</el-tag>
        </template>
      </el-table-column>

      <el-table-column :label="t('audit.source')" width="140">
        <template #default="{ row }">
          <div class="muted small">{{ row.ip ?? "—" }}</div>
          <div class="muted small mono">{{ row.request_id ?? "" }}</div>
        </template>
      </el-table-column>

      <el-table-column width="90" align="right">
        <template #default="{ row }">
          <el-button
            link
            size="small"
            data-test="toggle-detail"
            @click="expanded = expanded === row.id ? null : row.id"
          >
            {{ expanded === row.id ? t("audit.hide") : t("audit.detail") }}
          </el-button>
        </template>
      </el-table-column>

      <el-table-column type="expand">
        <template #default="{ row }">
          <pre class="detail" data-test="audit-detail">{{
            JSON.stringify({ before: row.before, after: row.after, detail: row.detail }, null, 2)
          }}</pre>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="hasMore" class="more">
      <el-button :loading="loading" data-test="load-more" @click="load(false)">
        {{ t("audit.loadMore") }}
      </el-button>
    </div>
    <p v-else-if="entries.length" class="muted end">{{ t("audit.end") }}</p>
    <p v-else-if="!loading" class="muted end">{{ t("audit.empty") }}</p>
  </div>
</template>

<style scoped>
.quick {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: var(--cairn-space-3);
}
.filters {
  display: grid;
  grid-template-columns: 2fr 1fr 2fr;
  gap: var(--cairn-space-3);
  margin-bottom: var(--cairn-space-4);
}
.mono {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 12px;
}
.muted {
  color: var(--cairn-text-muted);
}
.small {
  font-size: 11px;
}
.detail {
  margin: 0;
  padding: var(--cairn-space-3);
  background: var(--cairn-surface-sunken);
  border-radius: 6px;
  font-size: 12px;
  overflow-x: auto;
}
.more,
.end {
  margin-top: var(--cairn-space-4);
  text-align: center;
  font-size: 13px;
}
.mb {
  margin-bottom: var(--cairn-space-3);
}
</style>
