<script setup lang="ts">
/** Index versions: what is active, what is building, and what failed and why. */
import { RefreshCw } from "lucide-vue-next";
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { knowledge, type IndexProgress, type IndexVersion, type KnowledgeBase } from "@/api/knowledge";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import ProgressBar from "@/components/ui/ProgressBar.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import { formatDateTime, formatNumber } from "@/lib/utils";
import { describeError, isAbort } from "@/shared/errors";
import type { Tone } from "@/components/ui/types";

const props = defineProps<{ kb: KnowledgeBase; progress: IndexProgress | null; canManage: boolean }>();
const emit = defineEmits<{ rebuild: [] }>();
const { t, locale } = useI18n();

const versions = ref<IndexVersion[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);
const controller = new AbortController();

function tone(state: string): Tone {
  if (state === "active") return "ok";
  if (state === "building") return "info";
  if (state === "failed") return "bad";
  return "neutral";
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    versions.value = await knowledge.indexVersions(props.kb.id, controller.signal);
  } catch (caught) {
    if (!isAbort(caught)) error.value = describeError(caught).message;
  } finally {
    loading.value = false;
  }
}

onMounted(load);
watch(() => [props.kb.active_index_version, props.kb.building_index_version, props.progress?.chunk_done], () => void load());
onBeforeUnmount(() => controller.abort());
</script>

<template>
  <div class="grid gap-4" data-test="versions-panel">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <p class="max-w-[70ch] text-[13px] text-ink-2">{{ t("knowledge.versions.lede") }}</p>
      <div class="flex items-center gap-2">
        <Button variant="ghost" size="sm" @click="load"><RefreshCw aria-hidden="true" />{{ t("common.refresh") }}</Button>
        <Button v-if="canManage" variant="primary" size="sm" :disabled="!!kb.building_index_version" data-test="rebuild-button" @click="emit('rebuild')">
          {{ t("knowledge.rebuild.action") }}
        </Button>
      </div>
    </div>

    <ErrorState v-if="error" :message="error" @retry="load" />
    <Skeleton v-else-if="loading && !versions.length" :rows="3" />
    <EmptyState v-else-if="!versions.length" :title="t('knowledge.versions.empty')" compact />

    <ol v-else class="divide-y divide-line rounded-md border border-line" data-test="version-list">
      <li v-for="version in versions" :key="version.version" class="grid gap-2 px-3 py-3 sm:grid-cols-[100px_minmax(0,1fr)_auto] sm:items-center" :data-test="`version-${version.version}`">
        <div class="flex items-center gap-2">
          <span class="font-display text-[16px] font-semibold text-ink">v{{ version.version }}</span>
          <Badge :tone="tone(version.state)" size="sm" dot :pulse="version.state === 'building'">{{ t(`knowledge.versions.states.${version.state}`) }}</Badge>
        </div>
        <div class="min-w-0 text-[12.5px] text-ink-2">
          <p class="tnum">
            {{ t("knowledge.versions.chunks", { done: formatNumber(version.chunk_done, locale), total: formatNumber(version.chunk_total, locale) }) }}
            · {{ version.layout }}
          </p>
          <ProgressBar v-if="version.state === 'building'" :value="version.chunk_total ? (version.chunk_done / version.chunk_total) * 100 : 0" class="mt-1.5 max-w-sm" />
          <p v-if="version.error" class="mt-1 text-bad">{{ version.error }}</p>
        </div>
        <div class="tnum text-[12px] text-ink-3 sm:text-right">
          <p>{{ t("knowledge.versions.started") }} {{ formatDateTime(version.started_at, locale, "short") }}</p>
          <p v-if="version.completed_at">{{ t("knowledge.versions.completed") }} {{ formatDateTime(version.completed_at, locale, "short") }}</p>
        </div>
      </li>
    </ol>
  </div>
</template>
