<script setup lang="ts">
/**
 * Grants and effective permissions for one user (FR-B-03).
 *
 * Two lists, and the distinction is the point: grants are what was written
 * down (rows that can be revoked); effective permissions are what the system
 * evaluates after role-implied capabilities and implication (kb:manage
 * satisfies kb:read). One answers "what do I revoke?", the other "why can
 * they do that?".
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { type EffectivePermissionsResponse, type GrantResponse, type UserResponse, grants } from "@/api/admin";
import { knowledge, type KnowledgeBase } from "@/api/knowledge";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import Checkbox from "@/components/ui/Checkbox.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import NativeSelect from "@/components/ui/NativeSelect.vue";
import Notice from "@/components/ui/Notice.vue";
import Sheet from "@/components/ui/Sheet.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import { formatDateTime } from "@/lib/utils";
import Section from "@/shared/components/Section.vue";
import { useAsyncData } from "@/shared/composables/useAsyncData";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError } from "@/shared/errors";

const props = defineProps<{ user: UserResponse }>();
const emit = defineEmits<{ close: []; changed: [] }>();

const { t, locale } = useI18n();
const toasts = useToasts();
const open = ref(true);

const held = useAsyncData<GrantResponse[]>(() => grants.forUser(props.user.id));
const effective = useAsyncData<EffectivePermissionsResponse>(() => grants.effective(props.user.id));
const catalogue = useAsyncData<Record<string, string[]>>(() => grants.catalogue());
const bases = ref<KnowledgeBase[]>([]);
const basesLoading = ref(true);
const controller = new AbortController();

const adding = ref(false);
const error = ref<string | null>(null);
const form = ref({ resource_type: "knowledge_base", resource_id: "", permissions: [] as string[] });

const available = computed(() => catalogue.data.value?.[form.value.resource_type] ?? []);
const needsResourceId = computed(() => form.value.resource_type !== "workspace");
const canAdd = computed(
  () => !adding.value && form.value.permissions.length > 0 && (!needsResourceId.value || form.value.resource_id.trim().length > 0),
);
const kbName = computed(() => new Map(bases.value.map((kb) => [kb.id, kb.name])));

watch(
  () => form.value.resource_type,
  () => {
    form.value.permissions = [];
    form.value.resource_id = "";
  },
);

function togglePermission(permission: string, checked: boolean | "indeterminate"): void {
  form.value.permissions =
    checked === true ? [...new Set([...form.value.permissions, permission])] : form.value.permissions.filter((item) => item !== permission);
}

function resourceLabel(grant: GrantResponse): string {
  if (!grant.resource_id) return t("grants.workspaceWide");
  return kbName.value.get(grant.resource_id) ?? grant.resource_id;
}

async function addGrant(): Promise<void> {
  if (!canAdd.value) return;
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
    toasts.success(t("grants.granted"));
    emit("changed");
  } catch (caught) {
    error.value = describeError(caught).message;
  } finally {
    adding.value = false;
  }
}

async function revoke(grant: GrantResponse): Promise<void> {
  try {
    await grants.revoke(grant.id);
    await Promise.all([held.refresh(), effective.refresh()]);
    toasts.success(t("grants.revoked"));
    emit("changed");
  } catch (caught) {
    toasts.error(t("errors.unexpected"), describeError(caught).message);
  }
}

watch(open, (value) => {
  if (!value) emit("close");
});

onMounted(async () => {
  try {
    bases.value = (await knowledge.list(undefined, controller.signal)).items;
  } catch {
    bases.value = [];
  } finally {
    basesLoading.value = false;
  }
});
onBeforeUnmount(() => controller.abort());
</script>

<template>
  <Sheet v-model:open="open" :title="t('grants.title', { name: user.username })" size="lg" test-id="grants-drawer">
    <Section :title="t('grants.effective')" :description="t('grants.effectiveHint')" id="effective">
      <Skeleton v-if="effective.loading.value" :rows="2" />
      <div v-else-if="effective.data.value" class="grid gap-2" data-test="effective-permissions">
        <div v-if="effective.data.value.workspace_permissions.length" class="flex flex-wrap items-center gap-1.5">
          <span class="mr-1 text-[12px] text-ink-3">{{ t("grants.workspaceWide") }}</span>
          <Badge v-for="permission in effective.data.value.workspace_permissions" :key="permission" size="sm" tone="brand" mono>{{ permission }}</Badge>
        </div>
        <div v-for="(permissions, resource) in effective.data.value.resource_permissions" :key="resource" class="flex flex-wrap items-center gap-1.5">
          <span class="mr-1 max-w-56 truncate text-[12px] text-ink-2" :title="String(resource)">{{ kbName.get(String(resource)) ?? resource }}</span>
          <Badge v-for="permission in permissions" :key="permission" size="sm" tone="neutral" mono>{{ permission }}</Badge>
        </div>
        <p
          v-if="!effective.data.value.workspace_permissions.length && !Object.keys(effective.data.value.resource_permissions).length"
          class="text-[13px] text-ink-3"
        >
          {{ t("grants.none") }}
        </p>
      </div>
    </Section>

    <Section :title="t('grants.held')" :description="t('grants.heldHint')" id="held">
      <Skeleton v-if="held.loading.value" :rows="2" />
      <p v-else-if="!held.data.value?.length" class="text-[13px] text-ink-3">{{ t("grants.none") }}</p>
      <ul v-else class="divide-y divide-line rounded-md border border-line" data-test="grants-table">
        <li v-for="grant in held.data.value" :key="grant.id" class="flex items-start gap-3 px-3 py-2">
          <div class="min-w-0 flex-1">
            <p class="truncate text-[13px] text-ink">
              <span class="text-ink-3">{{ grant.resource_type }} ·</span> {{ resourceLabel(grant) }}
            </p>
            <p class="mt-1 flex flex-wrap gap-1">
              <Badge v-for="permission in grant.permissions" :key="permission" size="sm" tone="neutral" mono>{{ permission }}</Badge>
            </p>
            <p v-if="grant.is_break_glass" class="mt-1 text-[11.5px] text-warn">
              {{ t("grants.breakGlass", { at: formatDateTime(grant.expires_at, locale, "short") }) }}
            </p>
          </div>
          <Button size="sm" variant="ghost" class="text-bad" data-test="revoke-grant" @click="revoke(grant)">{{ t("grants.revoke") }}</Button>
        </li>
      </ul>
    </Section>

    <Section :title="t('grants.add')" id="add">
      <form class="grid gap-3" @submit.prevent="addGrant">
        <Notice v-if="error" tone="bad">{{ error }}</Notice>
        <div class="grid gap-3 sm:grid-cols-2">
          <Field :label="t('grants.resourceType')" v-slot="{ id }">
            <NativeSelect :id="id" v-model="form.resource_type" data-test="resource-type">
              <option v-for="(_permissions, type) in catalogue.data.value ?? {}" :key="type" :value="type">{{ t(`grants.resourceTypes.${type}`, String(type)) }}</option>
            </NativeSelect>
          </Field>
          <Field v-if="needsResourceId" :label="form.resource_type === 'knowledge_base' ? t('nav.knowledge') : t('grants.resourceId')" v-slot="{ id }">
            <NativeSelect
              v-if="form.resource_type === 'knowledge_base' && (basesLoading || bases.length)"
              :id="id"
              v-model="form.resource_id"
              :disabled="basesLoading"
              data-test="resource-id"
            >
              <option value="" disabled>{{ t("common.select") }}</option>
              <option v-for="kb in bases" :key="kb.id" :value="kb.id">{{ kb.name }}</option>
            </NativeSelect>
            <Input v-else :id="id" v-model="form.resource_id" data-test="resource-id" placeholder="kb_01HQZX3N9K2M5P7R8T" />
          </Field>
        </div>
        <Field :label="t('grants.permissions')">
          <div class="flex flex-wrap gap-x-4 gap-y-1.5" data-test="permissions">
            <Checkbox v-for="permission in available" :key="permission" :model-value="form.permissions.includes(permission)" @update:model-value="togglePermission(permission, $event)">
              <span class="font-mono text-[12.5px]">{{ permission }}</span>
            </Checkbox>
          </div>
        </Field>
        <div>
          <Button type="submit" variant="primary" size="sm" :disabled="!canAdd" :loading="adding" data-test="add-grant">{{ t("grants.grant") }}</Button>
        </div>
      </form>
    </Section>
  </Sheet>
</template>
