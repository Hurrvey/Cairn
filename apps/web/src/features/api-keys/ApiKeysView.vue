<script setup lang="ts">
/**
 * API key management (FR-B-14).
 *
 * The screen has one job beyond CRUD: make clear that a key's power is bounded
 * by its owner's. The explainer is not decoration — without it, "I gave the key
 * kb:manage but it can't manage" is a support ticket, when the actual behaviour
 * is the intersection working exactly as designed.
 */
import { computed, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";

import { type ApiKeyCreatedResponse, apiKeys } from "@/api/admin";
import { ApiError } from "@/api/client";
import PageHeader from "@/shared/components/PageHeader.vue";
import SecretReveal from "@/shared/components/SecretReveal.vue";
import { useAsyncData } from "@/shared/composables/useAsyncData";
import { usePermissions } from "@/shared/composables/usePermissions";

const { t } = useI18n();
const permissions = usePermissions();

const showAll = ref(false);
const list = useAsyncData(() => apiKeys.list(showAll.value));

const createOpen = ref(false);
const created = ref<ApiKeyCreatedResponse | null>(null);
const acknowledged = ref(false);
const submitting = ref(false);
const formError = ref<string | null>(null);

const SCOPES = ["kb:query", "kb:read", "kb:write", "kb:manage"];

const form = reactive({
  name: "",
  scopes: ["kb:query"] as string[],
  knowledge_base_ids: "" as string,
  rate_limit_rpm: null as number | null,
  ip_allowlist: "" as string,
});

const canSubmit = computed(
  () => !submitting.value && form.name.trim().length > 0 && form.scopes.length > 0,
);

function openCreate(): void {
  Object.assign(form, {
    name: "",
    scopes: ["kb:query"],
    knowledge_base_ids: "",
    rate_limit_rpm: null,
    ip_allowlist: "",
  });
  created.value = null;
  acknowledged.value = false;
  formError.value = null;
  createOpen.value = true;
}

function splitList(value: string): string[] {
  return value
    .split(/[\s,]+/)
    .map((part) => part.trim())
    .filter(Boolean);
}

async function submit(): Promise<void> {
  submitting.value = true;
  formError.value = null;
  try {
    const kbs = splitList(form.knowledge_base_ids);
    created.value = await apiKeys.create({
      name: form.name.trim(),
      scopes: form.scopes,
      knowledge_base_ids: kbs.length ? kbs : null,
      rate_limit_rpm: form.rate_limit_rpm,
      ip_allowlist: splitList(form.ip_allowlist).length
        ? splitList(form.ip_allowlist)
        : null,
      expires_at: null,
    });
    await list.refresh();
  } catch (error) {
    // GRANT_EXCEEDS_OWNER is the interesting failure: the request is rejected up
    // front rather than silently narrowed, because a key that quietly does less
    // than asked is worse than a clear error.
    formError.value = error instanceof ApiError ? error.detail : t("errors.unexpected");
  } finally {
    submitting.value = false;
  }
}

async function revoke(id: string): Promise<void> {
  await apiKeys.revoke(id);
  await list.refresh();
}

async function toggleScope(): Promise<void> {
  await list.refresh();
}
</script>

<template>
  <div>
    <PageHeader :title="t('keys.title')" :subtitle="t('keys.subtitle')">
      <template #actions>
        <el-checkbox
          v-if="permissions.canManageUsers.value"
          v-model="showAll"
          data-test="show-all"
          @change="toggleScope"
        >
          {{ t("keys.showAll") }}
        </el-checkbox>
        <el-button type="primary" data-test="new-key" @click="openCreate">
          {{ t("keys.new") }}
        </el-button>
      </template>
    </PageHeader>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="explainer"
      data-test="intersection-explainer"
    >
      {{ t("keys.intersectionExplainer") }}
    </el-alert>

    <el-table
      v-loading="list.loading.value"
      :data="list.data.value ?? []"
      data-test="keys-table"
      row-key="id"
    >
      <el-table-column :label="t('keys.name')" min-width="170">
        <template #default="{ row }">
          <strong>{{ row.name }}</strong>
          <div class="mono muted">{{ row.key_prefix }}…{{ row.last_four }}</div>
        </template>
      </el-table-column>

      <el-table-column :label="t('keys.scopes')" min-width="200">
        <template #default="{ row }">
          <el-tag v-for="s in row.scopes" :key="s" size="small" class="tag">{{ s }}</el-tag>
          <div v-if="row.knowledge_base_ids.length" class="muted small">
            {{ t("keys.scopedTo", { n: row.knowledge_base_ids.length }) }}
          </div>
          <div v-else class="muted small">{{ t("keys.allOwnerKbs") }}</div>
        </template>
      </el-table-column>

      <el-table-column :label="t('keys.usage')" width="150">
        <template #default="{ row }">
          <span class="muted">{{ t("keys.calls", { n: row.use_count }) }}</span>
          <div v-if="row.last_used_at" class="muted small">{{ row.last_used_at }}</div>
          <div v-else class="muted small">{{ t("keys.neverUsed") }}</div>
        </template>
      </el-table-column>

      <el-table-column :label="t('keys.status')" width="120">
        <template #default="{ row }">
          <el-tag v-if="row.revoked_at" size="small" type="danger">
            {{ t("keys.revoked") }}
          </el-tag>
          <el-tag v-else size="small" type="success">{{ t("keys.active") }}</el-tag>
        </template>
      </el-table-column>

      <el-table-column width="110" align="right">
        <template #default="{ row }">
          <el-popconfirm
            :title="t('keys.confirmRevoke')"
            :confirm-button-text="t('keys.revoke')"
            :cancel-button-text="t('common.cancel')"
            width="300"
            @confirm="revoke(row.id)"
          >
            <template #reference>
              <el-button link size="small" type="danger" :disabled="!!row.revoked_at">
                {{ t("keys.revoke") }}
              </el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="createOpen"
      :title="created ? t('keys.createdTitle') : t('keys.new')"
      width="500px"
      :close-on-click-modal="!created"
      data-test="create-key-dialog"
    >
      <template v-if="!created">
        <el-alert v-if="formError" type="error" :title="formError" show-icon :closable="false" class="mb" />
        <el-form label-position="top">
          <el-form-item :label="t('keys.name')">
            <el-input v-model="form.name" data-test="key-name" autofocus />
          </el-form-item>

          <el-form-item :label="t('keys.scopes')">
            <el-checkbox-group v-model="form.scopes" data-test="key-scopes">
              <el-checkbox v-for="s in SCOPES" :key="s" :value="s">{{ s }}</el-checkbox>
            </el-checkbox-group>
            <span class="hint">{{ t("keys.scopesHint") }}</span>
          </el-form-item>

          <el-form-item :label="t('keys.knowledgeBases')">
            <el-input
              v-model="form.knowledge_base_ids"
              data-test="key-kbs"
              placeholder="kb_01HQ… kb_02KM…"
            />
            <span class="hint">{{ t("keys.knowledgeBasesHint") }}</span>
          </el-form-item>

          <el-form-item :label="t('keys.rateLimit')">
            <el-input-number v-model="form.rate_limit_rpm" :min="1" :max="100000" data-test="key-rpm" />
          </el-form-item>

          <el-form-item :label="t('keys.ipAllowlist')">
            <el-input v-model="form.ip_allowlist" placeholder="10.0.0.0/8" data-test="key-cidr" />
          </el-form-item>
        </el-form>
      </template>

      <template v-else>
        <SecretReveal
          v-model:acknowledged="acknowledged"
          :value="created.key"
          :label="t('keys.keyFor', { name: created.api_key.name })"
          :hint="t('keys.keyHint')"
        />
      </template>

      <template #footer>
        <template v-if="!created">
          <el-button @click="createOpen = false">{{ t("common.cancel") }}</el-button>
          <el-button type="primary" :disabled="!canSubmit" :loading="submitting" data-test="submit" @click="submit">
            {{ t("common.create") }}
          </el-button>
        </template>
        <el-button
          v-else
          type="primary"
          :disabled="!acknowledged"
          data-test="done"
          @click="createOpen = false"
        >
          {{ t("common.done") }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.explainer {
  margin-bottom: var(--cairn-space-4);
}
.mono {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 12px;
}
.muted {
  color: var(--cairn-text-muted);
  font-size: 13px;
}
.small {
  font-size: 11px;
}
.tag {
  margin: 0 4px 2px 0;
}
.hint {
  display: block;
  font-size: 11px;
  color: var(--cairn-text-muted);
  line-height: 1.5;
  margin-top: 2px;
}
.mb {
  margin-bottom: var(--cairn-space-4);
}
</style>
