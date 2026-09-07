<script setup lang="ts">
/**
 * User management (FR-P-03).
 *
 * Two details carry weight here:
 *
 * 1. The generated initial password is shown once, in a dialog the operator
 *    must acknowledge. The account starts with a mandatory credential change,
 *    so this is a handover credential — not a password the administrator keeps.
 * 2. Deleting a user warns that it also revokes their API keys and grants,
 *    because that consequence is invisible from this screen otherwise.
 */
import { computed, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";

import { type CreatedUserResponse, type UserResponse, users } from "@/api/admin";
import { ApiError } from "@/api/client";
import GrantsDrawer from "@/features/users/GrantsDrawer.vue";
import PageHeader from "@/shared/components/PageHeader.vue";
import SecretReveal from "@/shared/components/SecretReveal.vue";
import { useAsyncData } from "@/shared/composables/useAsyncData";
import { useSessionStore } from "@/stores/session";

const { t } = useI18n();
const session = useSessionStore();

const list = useAsyncData(() => users.list({ limit: 200 }));

const createOpen = ref(false);
const created = ref<CreatedUserResponse | null>(null);
const acknowledged = ref(false);
const submitting = ref(false);
const formError = ref<string | null>(null);
const fieldErrors = ref<Record<string, string[]>>({});

const grantsFor = ref<UserResponse | null>(null);

const form = reactive({
  username: "",
  email: "",
  display_name: "",
  role: "user" as "admin" | "user",
});

const canSubmit = computed(() => !submitting.value && form.username.trim().length >= 3);

function openCreate(): void {
  Object.assign(form, { username: "", email: "", display_name: "", role: "user" });
  formError.value = null;
  fieldErrors.value = {};
  created.value = null;
  acknowledged.value = false;
  createOpen.value = true;
}

async function submit(): Promise<void> {
  submitting.value = true;
  formError.value = null;
  fieldErrors.value = {};
  try {
    created.value = await users.create({
      username: form.username.trim(),
      email: form.email.trim() || null,
      display_name: form.display_name.trim() || null,
      role: form.role,
    });
    await list.refresh();
  } catch (error) {
    if (error instanceof ApiError) {
      formError.value = error.detail;
      fieldErrors.value = error.byField();
    } else {
      formError.value = t("errors.unexpected");
    }
  } finally {
    submitting.value = false;
  }
}

function closeCreate(): void {
  createOpen.value = false;
  created.value = null;
}

async function toggleActive(user: UserResponse): Promise<void> {
  await users.update(user.id, { is_active: !user.is_active });
  await list.refresh();
}

async function forceChange(user: UserResponse): Promise<void> {
  await users.forcePasswordChange(user.id);
  await list.refresh();
}

async function remove(user: UserResponse): Promise<void> {
  await users.remove(user.id);
  await list.refresh();
}

const isSelf = (user: UserResponse): boolean => user.id === session.user?.id;
</script>

<template>
  <div>
    <PageHeader :title="t('users.title')" :subtitle="t('users.subtitle')">
      <template #actions>
        <el-button type="primary" data-test="new-user" @click="openCreate">
          {{ t("users.new") }}
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="list.error.value" type="error" :title="list.error.value" show-icon :closable="false" />

    <el-table
      v-loading="list.loading.value"
      :data="list.data.value ?? []"
      data-test="users-table"
      row-key="id"
    >
      <el-table-column :label="t('users.username')" min-width="180">
        <template #default="{ row }">
          <strong>{{ row.username }}</strong>
          <span v-if="isSelf(row)" class="you">{{ t("users.you") }}</span>
        </template>
      </el-table-column>

      <el-table-column :label="t('users.role')" width="120">
        <template #default="{ row }">
          <el-tag size="small" :type="row.role === 'admin' ? 'warning' : 'info'">
            {{ row.role }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column :label="t('users.status')" width="180">
        <template #default="{ row }">
          <span v-if="!row.is_active" class="muted">{{ t("users.disabled") }}</span>
          <span v-else-if="row.must_change_password" class="pending">
            {{ t("users.mustChange") }}
          </span>
          <span v-else class="ok">{{ t("users.active") }}</span>
        </template>
      </el-table-column>

      <el-table-column :label="t('users.lastLogin')" width="170">
        <template #default="{ row }">
          <span class="muted">{{ row.last_login_at ?? t("users.never") }}</span>
        </template>
      </el-table-column>

      <el-table-column width="290" align="right">
        <template #default="{ row }">
          <el-button link size="small" data-test="edit-grants" @click="grantsFor = row">
            {{ t("users.permissions") }}
          </el-button>
          <el-button link size="small" @click="forceChange(row)">
            {{ t("users.forceChange") }}
          </el-button>
          <el-button link size="small" @click="toggleActive(row)">
            {{ row.is_active ? t("users.disable") : t("users.enable") }}
          </el-button>
          <el-popconfirm
            :title="t('users.confirmDelete')"
            :confirm-button-text="t('common.delete')"
            :cancel-button-text="t('common.cancel')"
            width="280"
            @confirm="remove(row)"
          >
            <template #reference>
              <el-button link size="small" type="danger" :disabled="isSelf(row)">
                {{ t("common.delete") }}
              </el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <!-- create ------------------------------------------------------------ -->
    <el-dialog
      v-model="createOpen"
      :title="created ? t('users.createdTitle') : t('users.new')"
      width="460px"
      :close-on-click-modal="!created"
      data-test="create-user-dialog"
    >
      <template v-if="!created">
        <el-alert v-if="formError" type="error" :title="formError" show-icon :closable="false" class="mb" />
        <el-form label-position="top" @submit.prevent="submit">
          <el-form-item :label="t('users.username')" :error="fieldErrors.username?.[0]">
            <el-input v-model="form.username" data-test="username" autofocus />
          </el-form-item>
          <el-form-item :label="t('users.email')" :error="fieldErrors.email?.[0]">
            <el-input v-model="form.email" data-test="email" />
          </el-form-item>
          <el-form-item :label="t('users.displayName')">
            <el-input v-model="form.display_name" data-test="display-name" />
          </el-form-item>
          <el-form-item :label="t('users.role')">
            <el-radio-group v-model="form.role" data-test="role">
              <el-radio-button value="user">user</el-radio-button>
              <el-radio-button value="admin">admin</el-radio-button>
            </el-radio-group>
          </el-form-item>
        </el-form>
      </template>

      <template v-else>
        <SecretReveal
          v-if="created.initial_password"
          v-model:acknowledged="acknowledged"
          :value="created.initial_password"
          :label="t('users.initialPasswordFor', { name: created.user.username })"
          :hint="t('users.initialPasswordHint')"
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
          @click="closeCreate"
        >
          {{ t("common.done") }}
        </el-button>
      </template>
    </el-dialog>

    <GrantsDrawer
      v-if="grantsFor"
      :user="grantsFor"
      @close="grantsFor = null"
      @changed="list.refresh()"
    />
  </div>
</template>

<style scoped>
.mb {
  margin-bottom: var(--cairn-space-4);
}
.you {
  margin-left: 6px;
  font-size: 11px;
  color: var(--cairn-text-muted);
}
.muted {
  color: var(--cairn-text-muted);
  font-size: 13px;
}
.pending {
  color: var(--cairn-warning);
  font-size: 13px;
}
.ok {
  color: var(--cairn-success);
  font-size: 13px;
}
</style>
