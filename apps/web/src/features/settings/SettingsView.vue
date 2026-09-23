<script setup lang="ts">
/**
 * Workspace settings (FR-O-06).
 *
 * `admin_content_access` is the consequential one, so it gets a real
 * explanation of each option rather than a bare dropdown. Settings that
 * need a restart are labelled as such: a UI that implies a change took
 * effect when it did not is worse than one that refuses the change.
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { type SettingsResponse, queues, settings } from "@/api/admin";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import Notice from "@/components/ui/Notice.vue";
import RadioCards from "@/components/ui/RadioCards.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import { formatBytes, formatDuration } from "@/lib/utils";
import PageHeader from "@/shared/components/PageHeader.vue";
import Section from "@/shared/components/Section.vue";
import { useAsyncData } from "@/shared/composables/useAsyncData";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError } from "@/shared/errors";

const { t } = useI18n();
const toasts = useToasts();

const loaded = useAsyncData<SettingsResponse>(() => settings.get());
const queueStats = useAsyncData(() => queues.stats());

const draft = ref<Record<string, unknown>>({});
const saving = ref(false);
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
const accessMode = computed({
  get: () => String(draft.value.admin_content_access ?? "break_glass"),
  set: (value: string) => {
    draft.value.admin_content_access = value;
  },
});
const accessOptions = computed(() =>
  (["break_glass", "on_grant", "always"] as const).map((mode) => ({
    value: mode,
    label: t(`settings.access.${mode}.label`),
    description: t(`settings.access.${mode}.detail`),
    recommended: mode === "break_glass",
  })),
);

const NUMERIC_FIELDS = [
  { key: "break_glass_ttl_minutes", labelKey: "settings.breakGlassTtl", min: 5, max: 1440 },
  { key: "default_rate_limit_rpm", labelKey: "settings.defaultRpm", min: 1, max: 100000 },
  { key: "audit_retention_days", labelKey: "settings.auditRetention", min: 30, max: 3650 },
  { key: "session_ttl_minutes", labelKey: "settings.sessionTtl", min: 5, max: 10080 },
  { key: "max_upload_bytes", labelKey: "settings.maxUpload", min: 1048576, max: 10737418240 },
] as const;

function numberField(key: string): number | null {
  const value = draft.value[key];
  return typeof value === "number" ? value : null;
}

function setNumber(key: string, value: string | number | null): void {
  draft.value[key] = value === null || value === "" ? null : Number(value);
}

function ageTone(seconds: number): "ok" | "warn" | "bad" {
  if (seconds > 1800) return "bad";
  return seconds > 300 ? "warn" : "ok";
}

async function save(): Promise<void> {
  saving.value = true;
  error.value = null;
  try {
    const response = await settings.update(draft.value);
    restartNeeded.value = response.requires_restart ?? [];
    toasts.success(t("settings.saved"));
    await loaded.refresh();
  } catch (caught) {
    error.value = describeError(caught).message;
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div>
    <PageHeader :title="t('settings.title')" :description="t('settings.subtitle')" />

    <ErrorState v-if="loaded.error.value" :message="loaded.error.value" class="mb-4" @retry="loaded.refresh()" />
    <Notice v-if="restartNeeded.length" tone="warn" class="mb-4" test-id="restart-warning">
      {{ t("settings.restartNeeded", { keys: restartNeeded.join(", ") }) }}
    </Notice>

    <Skeleton v-if="loaded.loading.value && !loaded.data.value" :rows="5" />

    <form v-else-if="loaded.data.value" class="grid" @submit.prevent="save">
      <Section :title="t('settings.accessTitle')" :description="t('settings.accessDescription')" id="access">
        <RadioCards v-model="accessMode" :options="accessOptions" class="max-w-2xl" test-id="access-mode" />
      </Section>

      <Section :title="t('settings.limitsTitle')" :description="t('settings.limitsDescription')" id="limits">
        <div class="grid max-w-3xl gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Field v-for="field in NUMERIC_FIELDS" :key="field.key" :id="`setting-${field.key}`">
            <template #default="{ id }">
              <label :for="id" class="flex items-center gap-2 text-[12.5px] font-medium text-ink-2">
                {{ t(field.labelKey) }}
                <Badge size="sm" :tone="hotReloadable.has(field.key) ? 'ok' : 'warn'">
                  {{ hotReloadable.has(field.key) ? t("settings.hot") : t("settings.restart") }}
                </Badge>
              </label>
              <Input
                :id="id"
                :model-value="numberField(field.key)"
                type="number"
                :min="field.min"
                :max="field.max"
                class="mt-1.5"
                :data-test="`setting-${field.key}`"
                @update:model-value="setNumber(field.key, $event)"
              />
              <p v-if="field.key === 'max_upload_bytes'" class="mt-1 text-[12px] text-ink-3">= {{ formatBytes(numberField(field.key)) }}</p>
            </template>
          </Field>
        </div>
        <Notice v-if="error" tone="bad" class="mt-4 max-w-3xl">{{ error }}</Notice>
        <div class="mt-4">
          <Button type="submit" variant="primary" :loading="saving" data-test="save-settings">{{ t("common.save") }}</Button>
        </div>
      </Section>
    </form>

    <Section :title="t('settings.queuesTitle')" :description="t('settings.queuesHint')" id="queues">
      <Skeleton v-if="queueStats.loading.value && !queueStats.data.value" :rows="3" />
      <p v-else-if="queueStats.error.value" class="text-[13px] text-ink-3">{{ queueStats.error.value }}</p>
      <ul v-else class="divide-y divide-line rounded-md border border-line" data-test="queues-table">
        <li v-for="queue in queueStats.data.value?.queues ?? []" :key="queue.queue" class="grid grid-cols-[120px_1fr_1fr] items-center gap-3 px-3 py-2 text-[13px]">
          <code class="text-ink">{{ queue.queue }}</code>
          <span class="tnum text-ink-2">{{ t("settings.readyCount", { n: queue.ready }) }}</span>
          <span class="tnum flex items-center gap-2 text-ink-2">
            <Badge size="sm" :tone="queue.ready ? ageTone(queue.oldest_ready_age_seconds) : 'neutral'" dot>
              {{ queue.ready ? formatDuration(queue.oldest_ready_age_seconds) : t("settings.idle") }}
            </Badge>
          </span>
        </li>
      </ul>
    </Section>
  </div>
</template>
