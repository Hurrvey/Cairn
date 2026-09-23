<script setup lang="ts">
/**
 * User management (FR-P-03).
 *
 * The generated initial password is shown once and must be acknowledged; the
 * account starts with a mandatory credential change, so this is a handover
 * credential, not one the administrator keeps. Deleting a user says that it
 * also revokes their keys and grants, because that is invisible otherwise.
 */
import { MoreHorizontal, Plus, UserRound } from "lucide-vue-next";
import { computed, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";

import { type CreatedUserResponse, type UserResponse, users } from "@/api/admin";
import { ApiError } from "@/api/client";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import DropdownItem from "@/components/ui/DropdownItem.vue";
import DropdownMenu from "@/components/ui/DropdownMenu.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import Notice from "@/components/ui/Notice.vue";
import RadioCards from "@/components/ui/RadioCards.vue";
import Sheet from "@/components/ui/Sheet.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import GrantsSheet from "@/features/users/GrantsSheet.vue";
import { formatRelative } from "@/lib/utils";
import PageHeader from "@/shared/components/PageHeader.vue";
import SecretReveal from "@/shared/components/SecretReveal.vue";
import { useAsyncData } from "@/shared/composables/useAsyncData";
import { confirm } from "@/shared/composables/useConfirm";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError } from "@/shared/errors";
import { useSessionStore } from "@/stores/session";

const { t, locale } = useI18n();
const session = useSessionStore();
const toasts = useToasts();

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
const isSelf = (user: UserResponse): boolean => user.id === session.user?.id;

function openCreate(): void {
  Object.assign(form, { username: "", email: "", display_name: "", role: "user" });
  formError.value = null;
  fieldErrors.value = {};
  created.value = null;
  acknowledged.value = false;
  createOpen.value = true;
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return;
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
    formError.value = describeError(error).message;
    if (error instanceof ApiError) fieldErrors.value = error.byField();
  } finally {
    submitting.value = false;
  }
}

function closeCreate(): void {
  if (created.value && !acknowledged.value) return;
  createOpen.value = false;
  created.value = null;
}

async function toggleActive(user: UserResponse): Promise<void> {
  try {
    await users.update(user.id, { is_active: !user.is_active });
    toasts.success(user.is_active ? t("users.disabled") : t("users.active"));
    await list.refresh();
  } catch (caught) {
    toasts.error(t("errors.unexpected"), describeError(caught).message);
  }
}

async function forceChange(user: UserResponse): Promise<void> {
  const accepted = await confirm({
    title: t("users.forceChangeTitle", { name: user.username }),
    description: t("users.forceChangeBody"),
    confirmLabel: t("users.forceChange"),
  });
  if (!accepted) return;
  try {
    await users.forcePasswordChange(user.id);
    toasts.success(t("users.forceChangeDone"));
    await list.refresh();
  } catch (caught) {
    toasts.error(t("errors.unexpected"), describeError(caught).message);
  }
}

async function remove(user: UserResponse): Promise<void> {
  const accepted = await confirm({
    title: t("users.deleteTitle", { name: user.username }),
    description: t("users.confirmDelete"),
    confirmLabel: t("common.delete"),
    danger: true,
  });
  if (!accepted) return;
  try {
    await users.remove(user.id);
    toasts.success(t("users.deleted"));
    await list.refresh();
  } catch (caught) {
    toasts.error(t("common.deleteFailed"), describeError(caught).message);
  }
}
</script>

<template>
  <div>
    <PageHeader :title="t('users.title')" :description="t('users.subtitle')">
      <template #actions>
        <Button variant="primary" data-test="new-user" @click="openCreate">
          <Plus aria-hidden="true" />
          {{ t("users.new") }}
        </Button>
      </template>
    </PageHeader>

    <ErrorState v-if="list.error.value" :message="list.error.value" class="mb-4" @retry="list.refresh()" />
    <Skeleton v-if="list.loading.value && !list.data.value" :rows="4" />
    <EmptyState v-else-if="!list.data.value?.length && !list.error.value" :title="t('users.emptyTitle')" compact>
      <template #icon><UserRound /></template>
    </EmptyState>
    <div v-else class="overflow-hidden rounded-md border border-line" data-test="users-table">
      <div class="hidden grid-cols-[minmax(0,1.5fr)_100px_150px_140px_40px] gap-3 border-b border-line bg-surface-2 px-3 py-1.5 text-[11px] font-medium uppercase tracking-wide text-ink-3 md:grid">
        <span>{{ t("users.username") }}</span>
        <span>{{ t("users.role") }}</span>
        <span>{{ t("users.status") }}</span>
        <span>{{ t("users.lastLogin") }}</span>
        <span />
      </div>
      <ul class="divide-y divide-line">
        <li v-for="user in list.data.value" :key="user.id" class="grid grid-cols-[minmax(0,1fr)_40px] items-center gap-3 px-3 py-2.5 md:grid-cols-[minmax(0,1.5fr)_100px_150px_140px_40px]" :data-test="`user-${user.id}`">
          <div class="flex min-w-0 items-center gap-3">
            <span class="flex size-8 shrink-0 items-center justify-center rounded-full bg-surface-3 font-display text-[12px] font-semibold text-ink-2">
              {{ (user.display_name || user.username).slice(0, 2).toUpperCase() }}
            </span>
            <div class="min-w-0">
              <p class="truncate text-[13.5px] font-medium text-ink">
                {{ user.username }}
                <span v-if="isSelf(user)" class="ml-1 text-[11px] font-normal text-ink-3">{{ t("users.you") }}</span>
              </p>
              <p class="truncate text-[12px] text-ink-3">{{ [user.display_name, user.email].filter(Boolean).join(" · ") || "—" }}</p>
              <p class="mt-1 flex items-center gap-2 md:hidden">
                <Badge size="sm" :tone="user.role === 'admin' ? 'brand' : 'neutral'">{{ t(`users.roles.${user.role}`) }}</Badge>
                <Badge v-if="!user.is_active" size="sm" tone="neutral">{{ t("users.disabled") }}</Badge>
                <Badge v-else-if="user.must_change_password" size="sm" tone="warn">{{ t("users.mustChange") }}</Badge>
                <Badge v-else size="sm" tone="ok" dot>{{ t("users.active") }}</Badge>
              </p>
            </div>
          </div>
          <span class="hidden md:block"><Badge size="sm" :tone="user.role === 'admin' ? 'brand' : 'neutral'">{{ t(`users.roles.${user.role}`) }}</Badge></span>
          <span class="hidden md:block">
            <Badge v-if="!user.is_active" size="sm" tone="neutral">{{ t("users.disabled") }}</Badge>
            <Badge v-else-if="user.must_change_password" size="sm" tone="warn">{{ t("users.mustChange") }}</Badge>
            <Badge v-else size="sm" tone="ok" dot>{{ t("users.active") }}</Badge>
          </span>
          <span class="tnum hidden text-[12.5px] text-ink-2 md:block">{{ user.last_login_at ? formatRelative(user.last_login_at, locale) : t("users.never") }}</span>
          <DropdownMenu>
            <template #trigger>
              <Button variant="ghost" size="icon-sm" :aria-label="t('common.actions')" data-test="user-menu"><MoreHorizontal aria-hidden="true" /></Button>
            </template>
            <DropdownItem data-test="edit-grants" @select="grantsFor = user">{{ t("users.permissions") }}</DropdownItem>
            <DropdownItem @select="forceChange(user)">{{ t("users.forceChange") }}</DropdownItem>
            <DropdownItem :disabled="isSelf(user)" @select="toggleActive(user)">{{ user.is_active ? t("users.disable") : t("users.enable") }}</DropdownItem>
            <DropdownItem kind="separator" />
            <DropdownItem danger :disabled="isSelf(user)" @select="remove(user)">{{ t("common.delete") }}</DropdownItem>
          </DropdownMenu>
        </li>
      </ul>
    </div>

    <Sheet v-model:open="createOpen" :title="created ? t('users.createdTitle') : t('users.new')" test-id="create-user-dialog" @update:open="(value) => !value && closeCreate()">
      <template v-if="!created">
        <form class="grid gap-4" @submit.prevent="submit">
          <Notice v-if="formError" tone="bad" test-id="form-error">{{ formError }}</Notice>
          <Field :label="t('users.username')" required :error="fieldErrors.username" v-slot="{ id }">
            <Input :id="id" v-model="form.username" data-test="username" autofocus autocomplete="off" />
          </Field>
          <Field :label="t('users.email')" :error="fieldErrors.email" v-slot="{ id }">
            <Input :id="id" v-model="form.email" type="email" data-test="email" autocomplete="off" />
          </Field>
          <Field :label="t('users.displayName')" v-slot="{ id }">
            <Input :id="id" v-model="form.display_name" data-test="display-name" />
          </Field>
          <Field :label="t('users.role')">
            <RadioCards
              v-model="form.role"
              :options="[
                { value: 'user', label: t('users.roles.user'), description: t('users.roleHints.user') },
                { value: 'admin', label: t('users.roles.admin'), description: t('users.roleHints.admin') },
              ]"
              test-id="role"
            />
          </Field>
          <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
        </form>
      </template>
      <SecretReveal
        v-else-if="created.initial_password"
        v-model:acknowledged="acknowledged"
        :value="created.initial_password"
        :label="t('users.initialPasswordFor', { name: created.user.username })"
        :hint="t('users.initialPasswordHint')"
      />
      <template #footer>
        <template v-if="!created">
          <Button variant="ghost" @click="createOpen = false">{{ t("common.cancel") }}</Button>
          <Button variant="primary" :disabled="!canSubmit" :loading="submitting" data-test="submit" @click="submit">{{ t("common.create") }}</Button>
        </template>
        <Button v-else variant="primary" :disabled="!acknowledged" data-test="done" @click="closeCreate">{{ t("common.done") }}</Button>
      </template>
    </Sheet>

    <GrantsSheet v-if="grantsFor" :user="grantsFor" @close="grantsFor = null" @changed="list.refresh()" />
  </div>
</template>
