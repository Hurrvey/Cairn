<script setup lang="ts">
/**
 * Workspace settings (FR-O-06).
 *
 * `admin_content_access` is the consequential one, so it gets a real
 * explanation of each option rather than a bare dropdown: the difference
 * between `always` and `break_glass` is the difference between "we can host
 * your team's documents" and "we can't".
 *
 * Settings that need a restart are labelled as such. A UI that implies a change
 * took effect when it did not is worse than one that refuses the change.
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { type SettingsResponse, queues, settings } from "@/api/admin";
import { ApiError } from "@/api/client";
import PageHeader from "@/shared/components/PageHeader.vue";
import { useAsyncData } from "@/shared/composables/useAsyncData";

const { t } = useI18n();

const loaded = useAsyncData<SettingsResponse>(() => settings.get());
const queueStats = useAsyncData(() => queues.stats());

const draft = ref<Record<string, unknown>>({});
const saving = ref(false);
const message = ref<string | null>(null);
const error = ref<string | null>(null);
const restartNeeded = ref<string[]>([]);

watch(
  () => loaded.data.value,
  (value) => {
    if (value) draft.value = { ...value.settings };
  },
  { immediate: true },
);

const hotReloadable = computed(() => new Set(loaded.data.value?.hot_reloadable ?? []));

const ACCESS_MODES = ["break_glass", "on_grant", "always"] as const;

async function save(): Promise<void> {
  saving.value = true;
  error.value = null;
  message.value = null;
  try {
    const response = await settings.update(draft.value);
    restartNeeded.value = response.requires_restart;
    message.value = t("settings.saved");
    await loaded.refresh();
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught.detail : t("errors.unexpected");
  } finally {
    saving.value = false;
  }
}

function ageClass(seconds: number): string {
  if (seconds > 1800) return "bad";
  return seconds > 300 ? "warn" : "";
}
</script>

<template>
  <div>
    <PageHeader :title="t('settings.title')" :subtitle="t('settings.subtitle')" />

    <el-alert v-if="error" type="error" :title="error" show-icon :closable="false" class="mb" />
    <el-alert v-if="message" type="success" :title="message" show-icon :closable="false" class="mb" />
    <el-alert
      v-if="restartNeeded.length"
      type="warning"
      show-icon
      :closable="false"
      class="mb"
      data-test="restart-warning"
    >
      {{ t("settings.restartNeeded", { keys: restartNeeded.join(", ") }) }}
    </el-alert>

    <section v-if="loaded.data.value" class="card">
      <h2>{{ t("settings.accessTitle") }}</h2>

      <el-radio-group v-model="draft.admin_content_access" class="modes" data-test="access-mode">
        <label v-for="mode in ACCESS_MODES" :key="mode" class="mode">
          <el-radio :value="mode">
            <div>
              <strong>{{ t(`settings.access.${mode}.label`) }}</strong>
              <p>{{ t(`settings.access.${mode}.detail`) }}</p>
            </div>
          </el-radio>
        </label>
      </el-radio-group>

      <el-form label-position="top" class="grid">
        <el-form-item>
          <template #label>
            {{ t("settings.breakGlassTtl") }}
            <em v-if="hotReloadable.has('break_glass_ttl_minutes')" class="hot">
              {{ t("settings.hot") }}
            </em>
          </template>
          <el-input-number v-model="draft.break_glass_ttl_minutes" :min="5" :max="1440" />
        </el-form-item>

        <el-form-item>
          <template #label>
            {{ t("settings.defaultRpm") }}
            <em v-if="hotReloadable.has('default_rate_limit_rpm')" class="hot">
              {{ t("settings.hot") }}
            </em>
          </template>
          <el-input-number v-model="draft.default_rate_limit_rpm" :min="1" :max="100000" />
        </el-form-item>

        <el-form-item>
          <template #label>
            {{ t("settings.auditRetention") }}
            <em v-if="hotReloadable.has('audit_retention_days')" class="hot">
              {{ t("settings.hot") }}
            </em>
          </template>
          <el-input-number v-model="draft.audit_retention_days" :min="30" :max="3650" />
        </el-form-item>

        <el-form-item>
          <template #label>
            {{ t("settings.sessionTtl") }}
            <em class="cold">{{ t("settings.restart") }}</em>
          </template>
          <el-input-number v-model="draft.session_ttl_minutes" :min="5" :max="10080" />
        </el-form-item>
      </el-form>

      <el-button type="primary" :loading="saving" data-test="save-settings" @click="save">
        {{ t("common.save") }}
      </el-button>
    </section>

    <section class="card">
      <h2>{{ t("settings.queuesTitle") }}</h2>
      <p class="hint">{{ t("settings.queuesHint") }}</p>

      <el-table :data="queueStats.data.value?.queues ?? []" size="small" data-test="queues-table">
        <el-table-column prop="queue" :label="t('settings.queue')" width="140" />
        <el-table-column prop="ready" :label="t('settings.ready')" width="100" />
        <el-table-column :label="t('settings.oldestReady')">
          <template #default="{ row }">
            <span :class="ageClass(row.oldest_ready_age_seconds)">
              {{ row.oldest_ready_age_seconds }}s
            </span>
          </template>
        </el-table-column>
      </el-table>
    </section>
  </div>
</template>

<style scoped>
.card {
  background: var(--cairn-surface);
  border: 1px solid var(--cairn-border);
  border-radius: 8px;
  padding: var(--cairn-space-5);
  margin-bottom: var(--cairn-space-5);
}
h2 {
  margin: 0 0 var(--cairn-space-4);
  font-size: 15px;
}
.hint {
  margin: -8px 0 var(--cairn-space-4);
  font-size: 12px;
  color: var(--cairn-text-muted);
}
.modes {
  display: grid;
  gap: var(--cairn-space-3);
  margin-bottom: var(--cairn-space-5);
}
.mode p {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--cairn-text-muted);
  line-height: 1.5;
  white-space: normal;
  max-width: 62ch;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--cairn-space-4);
}
.hot,
.cold {
  font-style: normal;
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 3px;
  margin-left: 6px;
}
.hot {
  background: #e8f3ec;
  color: var(--cairn-success);
}
.cold {
  background: #f6efe4;
  color: var(--cairn-warning);
}
.warn {
  color: var(--cairn-warning);
}
.bad {
  color: var(--cairn-danger);
  font-weight: 600;
}
.mb {
  margin-bottom: var(--cairn-space-4);
}
</style>
