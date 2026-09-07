<script setup lang="ts">
/**
 * Grants and effective permissions for one user (FR-B-03).
 *
 * The drawer shows two things side by side, and the distinction is the point:
 *
 * * **Grants** are what was written down — individual rows that can be revoked.
 * * **Effective permissions** are what the system actually evaluates, after
 *   role-implied capabilities and implication (`kb:manage` satisfies `kb:read`).
 *
 * Showing only grants would leave an operator unable to answer "why can they do
 * that?"; showing only the effective set would leave them unable to answer
 * "what do I revoke?".
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import {
  type EffectivePermissionsResponse,
  type GrantResponse,
  type UserResponse,
  grants,
} from "@/api/admin";
import { ApiError } from "@/api/client";
import { useAsyncData } from "@/shared/composables/useAsyncData";

const props = defineProps<{ user: UserResponse }>();
const emit = defineEmits<{ close: []; changed: [] }>();

const { t } = useI18n();
const open = ref(true);

const held = useAsyncData<GrantResponse[]>(() => grants.forUser(props.user.id));
const effective = useAsyncData<EffectivePermissionsResponse>(() =>
  grants.effective(props.user.id),
);
const catalogue = useAsyncData<Record<string, string[]>>(() => grants.catalogue());

const adding = ref(false);
const error = ref<string | null>(null);
const form = ref({
  resource_type: "knowledge_base" as string,
  resource_id: "",
  permissions: [] as string[],
});

const available = computed(() => catalogue.data.value?.[form.value.resource_type] ?? []);
const needsResourceId = computed(() => form.value.resource_type !== "workspace");
const canAdd = computed(
  () =>
    !adding.value &&
    form.value.permissions.length > 0 &&
    (!needsResourceId.value || form.value.resource_id.trim().length > 0),
);

watch(
  () => form.value.resource_type,
  () => (form.value.permissions = []),
);

async function addGrant(): Promise<void> {
  adding.value = true;
  error.value = null;
  try {
    await grants.create({
      subject_type: "user",
      subject_id: props.user.id,
      resource_type: form.value.resource_type as never,
      resource_id: needsResourceId.value ? form.value.resource_id.trim() : null,
      permissions: form.value.permissions,
      expires_at: null,
      reason: null,
    });
    form.value.permissions = [];
    form.value.resource_id = "";
    await Promise.all([held.refresh(), effective.refresh()]);
    emit("changed");
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught.detail : t("errors.unexpected");
  } finally {
    adding.value = false;
  }
}

async function revoke(grant: GrantResponse): Promise<void> {
  await grants.revoke(grant.id);
  await Promise.all([held.refresh(), effective.refresh()]);
  emit("changed");
}

function close(): void {
  open.value = false;
  emit("close");
}
</script>

<template>
  <el-drawer
    v-model="open"
    :title="t('grants.title', { name: user.username })"
    size="560px"
    data-test="grants-drawer"
    @closed="close"
  >
    <section>
      <h3>{{ t("grants.effective") }}</h3>
      <p class="hint">{{ t("grants.effectiveHint") }}</p>

      <div v-if="effective.data.value" class="effective" data-test="effective-permissions">
        <div v-if="effective.data.value.workspace_permissions.length" class="group">
          <span class="scope">{{ t("grants.workspaceWide") }}</span>
          <div class="tags">
            <el-tag
              v-for="p in effective.data.value.workspace_permissions"
              :key="p"
              size="small"
              type="info"
              >{{ p }}</el-tag
            >
          </div>
        </div>

        <div
          v-for="(perms, resource) in effective.data.value.resource_permissions"
          :key="resource"
          class="group"
        >
          <span class="scope mono">{{ resource }}</span>
          <div class="tags">
            <el-tag v-for="p in perms" :key="p" size="small">{{ p }}</el-tag>
          </div>
        </div>

        <p
          v-if="
            !effective.data.value.workspace_permissions.length &&
            !Object.keys(effective.data.value.resource_permissions).length
          "
          class="empty"
        >
          {{ t("grants.none") }}
        </p>
      </div>
    </section>

    <el-divider />

    <section>
      <h3>{{ t("grants.held") }}</h3>
      <p class="hint">{{ t("grants.heldHint") }}</p>

      <el-table
        v-loading="held.loading.value"
        :data="held.data.value ?? []"
        size="small"
        data-test="grants-table"
      >
        <el-table-column :label="t('grants.resource')" min-width="200">
          <template #default="{ row }">
            <div class="mono">{{ row.resource_id ?? row.resource_type }}</div>
            <div v-if="row.is_break_glass" class="break-glass">
              {{ t("grants.breakGlass", { at: row.expires_at }) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('grants.permissions')" min-width="180">
          <template #default="{ row }">
            <el-tag v-for="p in row.permissions" :key="p" size="small" class="tag">{{ p }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column width="90" align="right">
          <template #default="{ row }">
            <el-button link size="small" type="danger" @click="revoke(row)">
              {{ t("grants.revoke") }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-divider />

    <section>
      <h3>{{ t("grants.add") }}</h3>
      <el-alert v-if="error" type="error" :title="error" show-icon :closable="false" class="mb" />

      <el-form label-position="top">
        <el-form-item :label="t('grants.resourceType')">
          <el-select v-model="form.resource_type" data-test="resource-type">
            <el-option
              v-for="(_perms, type) in catalogue.data.value ?? {}"
              :key="type"
              :label="type"
              :value="type"
            />
          </el-select>
        </el-form-item>

        <el-form-item v-if="needsResourceId" :label="t('grants.resourceId')">
          <el-input
            v-model="form.resource_id"
            data-test="resource-id"
            placeholder="kb_01HQZX3N9K2M5P7R8T"
          />
        </el-form-item>

        <el-form-item :label="t('grants.permissions')">
          <el-checkbox-group v-model="form.permissions" data-test="permissions">
            <el-checkbox v-for="p in available" :key="p" :value="p">{{ p }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>

        <el-button type="primary" :disabled="!canAdd" :loading="adding" data-test="add-grant" @click="addGrant">
          {{ t("grants.grant") }}
        </el-button>
      </el-form>
    </section>
  </el-drawer>
</template>

<style scoped>
h3 {
  margin: 0 0 4px;
  font-size: 14px;
}
.hint {
  margin: 0 0 var(--cairn-space-3);
  font-size: 12px;
  color: var(--cairn-text-muted);
  line-height: 1.5;
}
.effective {
  display: grid;
  gap: var(--cairn-space-3);
}
.group {
  display: grid;
  gap: 4px;
}
.scope {
  font-size: 12px;
  color: var(--cairn-text-muted);
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.tag {
  margin-right: 4px;
}
.mono {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 12px;
}
.break-glass {
  font-size: 11px;
  color: var(--cairn-warning);
}
.empty {
  font-size: 13px;
  color: var(--cairn-text-muted);
}
.mb {
  margin-bottom: var(--cairn-space-3);
}
</style>
