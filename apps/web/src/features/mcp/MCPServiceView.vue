<script setup lang="ts">
/**
 * MCP service: the managed listener that exposes knowledge bases as tools.
 *
 * Desired state and observed state are shown side by side because they can
 * legitimately differ for a few seconds after an action; the page tells the
 * user which one they are looking at instead of guessing.
 */
import { Download, Play, RefreshCw, RotateCcw, Square } from "lucide-vue-next";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";

import {
  type MCPConfig,
  type MCPLogFilters,
  type MCPLogItem,
  type MCPLogLevel,
  type MCPServiceAction,
  type MCPServiceSnapshot,
  type MCPServiceState,
  mcpService,
} from "@/api/mcp";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import CopyButton from "@/components/ui/CopyButton.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import KeyValue from "@/components/ui/KeyValue.vue";
import NativeSelect from "@/components/ui/NativeSelect.vue";
import Notice from "@/components/ui/Notice.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import Switch from "@/components/ui/Switch.vue";
import Textarea from "@/components/ui/Textarea.vue";
import { formatDateTime, formatRelative } from "@/lib/utils";
import PageHeader from "@/shared/components/PageHeader.vue";
import Section from "@/shared/components/Section.vue";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError, isAbort } from "@/shared/errors";
import { mcpTone } from "@/shared/pipeline";

const POLL_INTERVAL_MS = 2_000;
const HEARTBEAT_STALE_MS = 10_000;
const RECENT_LOG_LIMIT = 100;

const { t, locale } = useI18n();
const toasts = useToasts();

interface ConfigDraft {
  port: number;
  autoStart: boolean;
  allowedHosts: string;
  allowedOrigins: string;
  requestTimeoutS: number;
  maxRequestBytes: number;
}

const snapshot = ref<MCPServiceSnapshot | null>(null);
const draft = ref<ConfigDraft>({
  port: 8081,
  autoStart: true,
  allowedHosts: "",
  allowedOrigins: "",
  requestTimeoutS: 15,
  maxRequestBytes: 65_536,
});
const logItems = ref<MCPLogItem[]>([]);
const logLevel = ref<MCPLogLevel | "">("");
const logSince = ref("");
const liveFollow = ref(true);
const clock = ref(Date.now());
const loading = ref(true);
const saving = ref(false);
const actionBusy = ref<MCPServiceAction | null>(null);
const logsBusy = ref(false);
const error = ref<string | null>(null);

let disposed = false;
let pollInFlight = false;
let logsLoaded = false;
let pollTimer: ReturnType<typeof setTimeout> | undefined;
let pollController: AbortController | undefined;
let logController: AbortController | undefined;

// Only a managed supervisor ever applies a generation, so an unmanaged
// deployment is never "pending".
const pending = computed(
  () => snapshot.value !== null && snapshot.value.managed && snapshot.value.observed_generation !== snapshot.value.generation,
);

const actualState = computed<MCPServiceState>(() => {
  const current = snapshot.value;
  if (!current?.heartbeat_at) return "unknown";
  const heartbeat = Date.parse(current.heartbeat_at);
  if (!Number.isFinite(heartbeat)) return "unknown";
  if (clock.value - heartbeat > HEARTBEAT_STALE_MS) return "unknown";
  return current.state;
});

const configDirty = computed(() => {
  const current = snapshot.value;
  return current ? !sameConfig(configFromDraft(), current.config) : false;
});

const controlsDisabled = computed(
  () => !snapshot.value?.managed || pending.value || saving.value || actionBusy.value !== null,
);

const endpointsVisible = computed(
  () => actualState.value === "running" && snapshot.value?.effective_port !== null,
);

const pageHostname = window.location.hostname || "localhost";
const compatibilityEndpoint = `${window.location.origin}/mcp`;
const directEndpoint = computed(() =>
  snapshot.value?.effective_port === null ? "" : `http://${pageHostname}:${snapshot.value?.effective_port}/mcp`,
);

const downloadUrl = computed(() => mcpService.logsDownloadUrl({ limit: RECENT_LOG_LIMIT, ...selectedLogFilters() }));

const facts = computed(() => {
  const current = snapshot.value;
  if (!current) return [];
  return [
    { label: t("mcp.status.desired"), value: t(`mcp.states.${current.desired_state}`), testId: "desired-state" },
    { label: t("mcp.status.generation"), value: `${current.observed_generation ?? "—"} / ${current.generation}`, testId: "generation" },
    { label: t("mcp.status.effectivePort"), value: current.effective_port ?? "—" },
    { label: t("mcp.status.heartbeat"), value: current.heartbeat_at ? formatRelative(current.heartbeat_at, locale.value, clock.value) : "—" },
    { label: t("mcp.status.started"), value: formatDateTime(current.started_at, locale.value, "short") },
  ];
});

function lines(value: string): string[] {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function configFromDraft(): MCPConfig {
  return {
    port: Number(draft.value.port),
    auto_start: draft.value.autoStart,
    allowed_hosts: lines(draft.value.allowedHosts),
    allowed_origins: lines(draft.value.allowedOrigins),
    request_timeout_s: Number(draft.value.requestTimeoutS),
    max_request_bytes: Number(draft.value.maxRequestBytes),
  };
}

function sameConfig(left: MCPConfig, right: MCPConfig): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function syncDraft(config: MCPConfig): void {
  draft.value = {
    port: config.port,
    autoStart: config.auto_start,
    allowedHosts: config.allowed_hosts.join("\n"),
    allowedOrigins: config.allowed_origins.join("\n"),
    requestTimeoutS: config.request_timeout_s,
    maxRequestBytes: config.max_request_bytes,
  };
}

function applySnapshot(next: MCPServiceSnapshot, forceDraft = false): void {
  const shouldSyncDraft = forceDraft || !snapshot.value || !configDirty.value;
  snapshot.value = next;
  if (shouldSyncDraft) syncDraft(next.config);
}

function selectedLogFilters(): Pick<MCPLogFilters, "level" | "since"> {
  const filters: Pick<MCPLogFilters, "level" | "since"> = {};
  if (logLevel.value) filters.level = logLevel.value;
  if (logSince.value) {
    const parsed = new Date(logSince.value);
    if (!Number.isNaN(parsed.getTime())) filters.since = parsed.toISOString();
  }
  return filters;
}

async function loadLogs(signal: AbortSignal, append: boolean): Promise<void> {
  const afterId = append ? logItems.value.reduce((highest, item) => Math.max(highest, item.id), 0) : 0;
  const response = await mcpService.logs({ after_id: afterId, limit: RECENT_LOG_LIMIT, ...selectedLogFilters() }, signal);
  if (append && afterId > 0) {
    const known = new Set(logItems.value.map((item) => item.id));
    logItems.value = [...logItems.value, ...response.items.filter((item) => !known.has(item.id))].slice(-RECENT_LOG_LIMIT);
  } else {
    logItems.value = response.items;
  }
  logsLoaded = true;
}

async function poll(): Promise<void> {
  if (disposed || pollInFlight) return;
  pollInFlight = true;
  pollController = new AbortController();
  clock.value = Date.now();
  try {
    const next = await mcpService.get(pollController.signal);
    applySnapshot(next);
    if (!logsLoaded || liveFollow.value) {
      await loadLogs(pollController.signal, liveFollow.value);
    }
    error.value = "";
  } catch (caught) {
    if (!isAbort(caught)) error.value = describeError(caught).message;
  } finally {
    loading.value = false;
    pollInFlight = false;
    pollController = undefined;
    if (!disposed) pollTimer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
  }
}

async function refreshLogs(): Promise<void> {
  logController?.abort();
  logController = new AbortController();
  logsBusy.value = true;
  try {
    await loadLogs(logController.signal, false);
  } catch (caught) {
    if (!isAbort(caught)) error.value = describeError(caught).message;
  } finally {
    logsBusy.value = false;
    logController = undefined;
  }
}

async function saveConfig(): Promise<void> {
  if (!snapshot.value || controlsDisabled.value || !configDirty.value) return;
  saving.value = true;
  error.value = "";
  try {
    const next = await mcpService.updateConfig(snapshot.value.generation, configFromDraft());
    applySnapshot(next, true);
    toasts.success(t("mcp.config.saved"));
  } catch (caught) {
    error.value = describeError(caught).message;
  } finally {
    saving.value = false;
  }
}

async function runAction(action: MCPServiceAction): Promise<void> {
  if (!snapshot.value || controlsDisabled.value) return;
  actionBusy.value = action;
  error.value = "";
  try {
    const next = await mcpService.act(snapshot.value.generation, action);
    applySnapshot(next);
    toasts.info(t("mcp.actions.accepted", { action: t(`mcp.actions.${action}`) }));
  } catch (caught) {
    error.value = describeError(caught).message;
  } finally {
    actionBusy.value = null;
  }
}

function levelTone(level: MCPLogLevel): "bad" | "warn" | "neutral" {
  if (level === "ERROR") return "bad";
  return level === "WARNING" ? "warn" : "neutral";
}

onMounted(() => void poll());

onBeforeUnmount(() => {
  disposed = true;
  if (pollTimer) clearTimeout(pollTimer);
  pollController?.abort();
  logController?.abort();
});
</script>

<template>
  <div>
    <PageHeader :title="t('mcp.title')" :description="t('mcp.subtitle')">
      <template #badges>
        <Badge v-if="snapshot" :tone="mcpTone(actualState)" dot :pulse="actualState === 'running'" data-test="actual-state">
          {{ t(`mcp.states.${actualState}`) }}
        </Badge>
        <Badge v-if="pending" tone="warn" data-test="pending">{{ t("mcp.status.pending") }}</Badge>
      </template>
      <template #actions>
        <Button variant="ghost" size="icon" :aria-label="t('common.refresh')" :loading="loading" @click="poll"><RefreshCw aria-hidden="true" /></Button>
        <Button
          variant="primary"
          :disabled="controlsDisabled || ['running', 'starting'].includes(actualState)"
          :loading="actionBusy === 'start'"
          data-test="action-start"
          @click="runAction('start')"
        >
          <Play aria-hidden="true" />
          {{ t("mcp.actions.start") }}
        </Button>
        <Button
          variant="secondary"
          :disabled="controlsDisabled || ['stopped', 'stopping'].includes(actualState)"
          :loading="actionBusy === 'stop'"
          data-test="action-stop"
          @click="runAction('stop')"
        >
          <Square aria-hidden="true" />
          {{ t("mcp.actions.stop") }}
        </Button>
        <Button
          variant="secondary"
          :disabled="controlsDisabled || actualState !== 'running'"
          :loading="actionBusy === 'restart'"
          data-test="action-restart"
          @click="runAction('restart')"
        >
          <RotateCcw aria-hidden="true" />
          {{ t("mcp.actions.restart") }}
        </Button>
      </template>
    </PageHeader>

    <ErrorState v-if="error" :message="error" class="mb-4" @retry="poll" />
    <Notice v-if="snapshot && !snapshot.managed" tone="warn" class="mb-4" test-id="unmanaged-notice">{{ t("mcp.deploymentNotConfigured") }}</Notice>
    <Notice v-if="snapshot?.last_error" tone="bad" class="mb-4" :title="t('mcp.status.lastError')">{{ snapshot.last_error }}</Notice>

    <Skeleton v-if="loading && !snapshot" :rows="6" />

    <template v-else-if="snapshot">
      <Section :title="t('mcp.status.title')" id="status">
        <KeyValue :items="facts" :columns="3" />
      </Section>

      <Section v-if="endpointsVisible" :title="t('mcp.endpoints.title')" :description="t('mcp.endpoints.hint')" id="endpoints">
        <div class="grid gap-2 md:grid-cols-2">
          <div class="flex items-center gap-3 rounded-md border border-line px-3 py-2" data-test="compat-endpoint">
            <div class="min-w-0 flex-1">
              <p class="text-[12px] text-ink-3">{{ t("mcp.endpoints.compatibility") }}</p>
              <code class="block truncate text-[12.5px] text-ink">{{ compatibilityEndpoint }}</code>
            </div>
            <CopyButton :value="compatibilityEndpoint" />
          </div>
          <div class="flex items-center gap-3 rounded-md border border-line px-3 py-2" data-test="direct-endpoint">
            <div class="min-w-0 flex-1">
              <p class="text-[12px] text-ink-3">{{ t("mcp.endpoints.direct") }}</p>
              <code class="block truncate text-[12.5px] text-ink">{{ directEndpoint }}</code>
            </div>
            <CopyButton :value="directEndpoint" />
          </div>
        </div>
      </Section>

      <Section :title="t('mcp.config.title')" :description="t('mcp.config.hint')" id="config">
        <form class="grid max-w-2xl gap-3" @submit.prevent="saveConfig">
          <div class="grid gap-3 sm:grid-cols-2">
            <Field :label="t('mcp.config.port')" v-slot="{ id }">
              <NativeSelect :id="id" v-model="draft.port" :disabled="!snapshot.managed" data-test="config-port">
                <option v-for="port in snapshot.allowed_ports" :key="port" :value="port">{{ port }}</option>
              </NativeSelect>
            </Field>
            <Field :label="t('mcp.config.autoStart')">
              <Switch v-model="draft.autoStart" :disabled="!snapshot.managed" :label="draft.autoStart ? t('common.on') : t('common.off')" class="h-8" />
            </Field>
            <Field :label="t('mcp.config.timeout')" v-slot="{ id }">
              <Input :id="id" v-model="draft.requestTimeoutS" type="number" min="1" max="60" :disabled="!snapshot.managed" />
            </Field>
            <Field :label="t('mcp.config.maxBytes')" v-slot="{ id }">
              <Input :id="id" v-model="draft.maxRequestBytes" type="number" min="1024" max="1048576" step="1024" :disabled="!snapshot.managed" />
            </Field>
            <Field :label="t('mcp.config.hosts')" :hint="t('mcp.config.hostsHint')" v-slot="{ id }">
              <Textarea :id="id" v-model="draft.allowedHosts" rows="3" mono :disabled="!snapshot.managed" />
            </Field>
            <Field :label="t('mcp.config.origins')" :hint="t('mcp.config.originsHint')" v-slot="{ id }">
              <Textarea :id="id" v-model="draft.allowedOrigins" rows="3" mono :disabled="!snapshot.managed" />
            </Field>
          </div>
          <div>
            <Button type="submit" variant="primary" :loading="saving" :disabled="controlsDisabled || !configDirty" data-test="save-config">{{ t("mcp.config.save") }}</Button>
          </div>
        </form>
      </Section>

      <Section :title="t('mcp.logs.title')" :description="t('mcp.logs.hint')" id="logs">
        <template #actions>
          <Button variant="outline" size="sm" :href="downloadUrl" download data-test="download-logs">
            <Download aria-hidden="true" />
            {{ t("mcp.logs.download") }}
          </Button>
        </template>
        <div class="mb-3 flex flex-wrap items-center gap-2">
          <NativeSelect v-model="logLevel" size="sm" class="w-40" :aria-label="t('mcp.logs.level')">
            <option value="">{{ t("mcp.logs.level") }}</option>
            <option value="INFO">{{ t("mcp.logs.levels.info") }}</option>
            <option value="WARNING">{{ t("mcp.logs.levels.warning") }}</option>
            <option value="ERROR">{{ t("mcp.logs.levels.error") }}</option>
          </NativeSelect>
          <Input v-model="logSince" size="sm" type="datetime-local" class="w-56" :aria-label="t('mcp.logs.since')" />
          <Button size="sm" variant="secondary" :loading="logsBusy" @click="refreshLogs">{{ t("mcp.logs.apply") }}</Button>
          <Switch v-model="liveFollow" :label="t('mcp.logs.liveFollow')" />
        </div>
        <EmptyState v-if="!logItems.length" :title="t('mcp.logs.empty')" compact />
        <ol v-else class="divide-y divide-line rounded-md border border-line" role="log" aria-live="polite">
          <li v-for="item in logItems" :key="item.id" class="px-3 py-2">
            <div class="flex flex-wrap items-center gap-2 text-[11.5px] text-ink-3">
              <time :datetime="item.at" class="tnum">{{ formatDateTime(item.at, locale, "short") }}</time>
              <Badge size="sm" :tone="levelTone(item.level)">{{ item.level }}</Badge>
              <code class="text-ink">{{ item.event }}</code>
              <span v-if="item.request_id" class="font-mono">{{ item.request_id }}</span>
              <span v-if="item.status !== null" class="tnum">{{ t("mcp.logs.status") }} {{ item.status }}</span>
              <span v-if="item.duration_ms !== null" class="tnum">{{ item.duration_ms }} ms</span>
            </div>
            <p class="mt-1 whitespace-pre-wrap break-words text-[13px] text-ink" data-test="log-detail">{{ item.detail }}</p>
          </li>
        </ol>
      </Section>
    </template>
  </div>
</template>
