<script setup lang="ts">
/**
 * API keys (FR-B-14) and how to connect with them.
 *
 * A key's power is bounded by its owner's: the effective permission is the
 * intersection, recomputed on every request. That is why revoking a user's
 * access narrows all of their keys immediately, and why the create form
 * refuses scopes the owner does not hold instead of quietly narrowing them.
 */
import { KeyRound, MoreHorizontal, Plus } from "lucide-vue-next";
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";

import { type ApiKeyCreatedResponse, type ApiKeyResponse, apiKeys } from "@/api/admin";
import { knowledge, type KnowledgeBase } from "@/api/knowledge";
import { meta } from "@/api/meta";
import Badge from "@/components/ui/Badge.vue";
import Button from "@/components/ui/Button.vue";
import Checkbox from "@/components/ui/Checkbox.vue";
import CopyButton from "@/components/ui/CopyButton.vue";
import DropdownItem from "@/components/ui/DropdownItem.vue";
import DropdownMenu from "@/components/ui/DropdownMenu.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import ErrorState from "@/components/ui/ErrorState.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import Notice from "@/components/ui/Notice.vue";
import Sheet from "@/components/ui/Sheet.vue";
import Skeleton from "@/components/ui/Skeleton.vue";
import Switch from "@/components/ui/Switch.vue";
import { formatNumber, formatRelative, splitLines } from "@/lib/utils";
import PageHeader from "@/shared/components/PageHeader.vue";
import SecretReveal from "@/shared/components/SecretReveal.vue";
import Section from "@/shared/components/Section.vue";
import { confirm } from "@/shared/composables/useConfirm";
import { usePermissions } from "@/shared/composables/usePermissions";
import { useToasts } from "@/shared/composables/useToasts";
import { describeError, isAbort } from "@/shared/errors";

const { t, locale } = useI18n();
const permissions = usePermissions();
const toasts = useToasts();

const SCOPES = ["kb:query", "kb:read", "kb:write", "kb:manage"] as const;

const rows = ref<ApiKeyResponse[]>([]);
const bases = ref<KnowledgeBase[]>([]);
const capabilities = ref<Record<string, boolean>>({});
const showAll = ref(false);
const loading = ref(true);
const error = ref<{ message: string; requestId?: string } | null>(null);
const controller = new AbortController();

const createOpen = ref(false);
const created = ref<ApiKeyCreatedResponse | null>(null);
const acknowledged = ref(false);
const submitting = ref(false);
const formError = ref<string | null>(null);
const form = reactive({
  name: "",
  scopes: ["kb:query"] as string[],
  knowledge_base_ids: [] as string[],
  rate_limit_rpm: null as number | null,
  ip_allowlist: "",
  expires_at: "",
});

const origin = window.location.origin;
const retrievalEndpoint = `${origin}/v1/retrieval/query`;
const mcpEndpoint = `${origin}/mcp`;
const exampleKb = computed(() => bases.value[0]?.id ?? "kb_…");
const curlExample = computed(
  () =>
    `curl -X POST ${retrievalEndpoint} \\\n  -H "Authorization: Bearer <api-key>" \\\n  -H "Content-Type: application/json" \\\n  -d '{"targets":[{"knowledge_base_id":"${exampleKb.value}"}],"query":"…","search_mode":"hybrid","top_k":5}'`,
);
const kbName = computed(() => new Map(bases.value.map((kb) => [kb.id, kb.name])));
const active = computed(() => rows.value.filter((row) => !row.revoked_at));
const revoked = computed(() => rows.value.filter((row) => !!row.revoked_at));
const canSubmit = computed(() => !submitting.value && form.name.trim().length > 0 && form.scopes.length > 0);

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    rows.value = await apiKeys.list(showAll.value);
  } catch (caught) {
    if (!isAbort(caught)) {
      const failure = describeError(caught);
      error.value = failure.requestId ? { message: failure.message, requestId: failure.requestId } : { message: failure.message };
    }
  } finally {
    loading.value = false;
  }
}

async function loadContext(): Promise<void> {
  try {
    bases.value = (await knowledge.list(undefined, controller.signal)).items;
  } catch {
    bases.value = [];
  }
  try {
    capabilities.value = (await meta()).capabilities;
  } catch {
    capabilities.value = {};
  }
}

function openCreate(): void {
  Object.assign(form, { name: "", scopes: ["kb:query"], knowledge_base_ids: [], rate_limit_rpm: null, ip_allowlist: "", expires_at: "" });
  created.value = null;
  acknowledged.value = false;
  formError.value = null;
  createOpen.value = true;
}

function toggleScope(scope: string, checked: boolean | "indeterminate"): void {
  form.scopes = checked === true ? [...new Set([...form.scopes, scope])] : form.scopes.filter((item) => item !== scope);
}

function toggleKb(id: string, checked: boolean | "indeterminate"): void {
  form.knowledge_base_ids = checked === true ? [...new Set([...form.knowledge_base_ids, id])] : form.knowledge_base_ids.filter((item) => item !== id);
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return;
  submitting.value = true;
  formError.value = null;
  try {
    const allowlist = splitLines(form.ip_allowlist);
    created.value = await apiKeys.create({
      name: form.name.trim(),
      scopes: form.scopes,
      knowledge_base_ids: form.knowledge_base_ids.length ? form.knowledge_base_ids : null,
      rate_limit_rpm: form.rate_limit_rpm,
      ip_allowlist: allowlist.length ? allowlist : null,
      expires_at: form.expires_at ? new Date(form.expires_at).toISOString() : null,
    });
    await load();
  } catch (caught) {
    // GRANT_EXCEEDS_OWNER is the interesting failure: the request is rejected up
    // front rather than silently narrowed, because a key that quietly does less
    // than asked is worse than a clear error.
    formError.value = describeError(caught).message;
  } finally {
    submitting.value = false;
  }
}

function closeCreate(): void {
  if (created.value && !acknowledged.value) return;
  createOpen.value = false;
  created.value = null;
}

async function revoke(key: ApiKeyResponse): Promise<void> {
  const accepted = await confirm({
    title: t("keys.revokeTitle", { name: key.name }),
    description: t("keys.confirmRevoke"),
    confirmLabel: t("keys.revoke"),
    danger: true,
  });
  if (!accepted) return;
  try {
    await apiKeys.revoke(key.id);
    toasts.success(t("keys.revoked"));
    await load();
  } catch (caught) {
    toasts.error(t("keys.revokeFailed"), describeError(caught).message);
  }
}

onMounted(() => {
  void load();
  void loadContext();
});
onBeforeUnmount(() => controller.abort());
</script>

<template>
  <div>
    <PageHeader :title="t('keys.title')" :description="t('keys.subtitle')">
      <template #actions>
        <Switch v-if="permissions.canManageUsers.value" v-model="showAll" :label="t('keys.showAll')" data-test="show-all" @update:model-value="load" />
        <Button variant="primary" data-test="new-key" @click="openCreate">
          <Plus aria-hidden="true" />
          {{ t("keys.new") }}
        </Button>
      </template>
    </PageHeader>

    <ErrorState v-if="error" :message="error.message" :request-id="error.requestId" class="mb-4" @retry="load" />

    <Section :title="t('keys.list')" id="keys">
      <Skeleton v-if="loading" :rows="3" />
      <EmptyState v-else-if="!rows.length && !error" :title="t('keys.emptyTitle')" :description="t('keys.emptyBody')" compact>
        <template #icon><KeyRound /></template>
        <Button variant="primary" size="sm" @click="openCreate">{{ t("keys.new") }}</Button>
      </EmptyState>
      <template v-else>
        <ul class="divide-y divide-line rounded-md border border-line" data-test="keys-table">
          <li v-for="key in [...active, ...revoked]" :key="key.id" :class="['grid gap-2 px-3 py-2.5 sm:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_auto_auto] sm:items-center', key.revoked_at && 'opacity-60']" :data-test="`key-${key.id}`">
            <div class="min-w-0">
              <p class="flex items-center gap-2 text-[13.5px] font-medium text-ink">
                <span class="truncate">{{ key.name }}</span>
                <Badge v-if="key.revoked_at" size="sm" tone="bad">{{ t("keys.revokedState") }}</Badge>
                <Badge v-else-if="key.expires_at && new Date(key.expires_at) < new Date()" size="sm" tone="warn">{{ t("keys.expired") }}</Badge>
                <Badge v-else size="sm" tone="ok" dot>{{ t("keys.active") }}</Badge>
              </p>
              <p class="font-mono text-[12px] text-ink-3">{{ key.key_prefix }}…{{ key.last_four }}</p>
            </div>
            <div class="min-w-0 text-[12px] text-ink-2">
              <p class="flex flex-wrap gap-1">
                <Badge v-for="scope in key.scopes" :key="scope" size="sm" tone="neutral" mono>{{ scope }}</Badge>
              </p>
              <p class="mt-1 truncate">
                <template v-if="key.knowledge_base_ids.length">
                  {{ key.knowledge_base_ids.map((id) => kbName.get(id) ?? id).join(", ") }}
                </template>
                <template v-else>{{ t("keys.allOwnerKbs") }}</template>
              </p>
            </div>
            <div class="tnum text-[12px] text-ink-3 sm:text-right">
              <p>{{ t("keys.calls", { n: formatNumber(key.use_count, locale) }) }}</p>
              <p>{{ key.last_used_at ? formatRelative(key.last_used_at, locale) : t("keys.neverUsed") }}</p>
            </div>
            <DropdownMenu>
              <template #trigger>
                <Button variant="ghost" size="icon-sm" :aria-label="t('common.actions')" :disabled="!!key.revoked_at"><MoreHorizontal aria-hidden="true" /></Button>
              </template>
              <DropdownItem danger data-test="revoke-key" @select="revoke(key)">{{ t("keys.revoke") }}</DropdownItem>
            </DropdownMenu>
          </li>
        </ul>
        <p class="mt-2 text-[12px] text-ink-3">{{ t("keys.intersectionExplainer") }}</p>
      </template>
    </Section>

    <Section :title="t('keys.connect.title')" :description="t('keys.connect.description')" id="connect">
      <div class="grid gap-3">
        <div class="flex items-center gap-3 rounded-md border border-line px-3 py-2">
          <div class="min-w-0 flex-1">
            <p class="text-[12px] text-ink-3">{{ t("dashboard.connect.retrieval") }}</p>
            <code class="block truncate text-[12.5px]">{{ retrievalEndpoint }}</code>
          </div>
          <CopyButton :value="retrievalEndpoint" />
        </div>
        <div v-if="capabilities.mcp" class="flex items-center gap-3 rounded-md border border-line px-3 py-2">
          <div class="min-w-0 flex-1">
            <p class="text-[12px] text-ink-3">{{ t("dashboard.connect.mcp") }}</p>
            <code class="block truncate text-[12.5px]">{{ mcpEndpoint }}</code>
          </div>
          <CopyButton :value="mcpEndpoint" />
        </div>
        <div class="relative rounded-md border border-line bg-surface-2">
          <pre class="overflow-x-auto px-3 py-2.5 text-[12px] leading-relaxed text-ink" data-test="curl-example">{{ curlExample }}</pre>
          <CopyButton :value="curlExample" class="absolute right-1.5 top-1.5" />
        </div>
      </div>
    </Section>

    <Sheet v-model:open="createOpen" :title="created ? t('keys.createdTitle') : t('keys.new')" :description="created ? undefined : t('keys.createHint')" test-id="create-key-dialog" @update:open="(value) => !value && closeCreate()">
      <template v-if="!created">
        <form class="grid gap-4" @submit.prevent="submit">
          <Notice v-if="formError" tone="bad" test-id="form-error">{{ formError }}</Notice>
          <Field :label="t('common.name')" required v-slot="{ id }">
            <Input :id="id" v-model="form.name" data-test="key-name" autofocus :placeholder="t('keys.namePlaceholder')" />
          </Field>
          <Field :label="t('keys.scopes')" :hint="t('keys.scopesHint')">
            <div class="grid gap-1.5" data-test="key-scopes">
              <Checkbox v-for="scope in SCOPES" :key="scope" :model-value="form.scopes.includes(scope)" :value="scope" @update:model-value="toggleScope(scope, $event)">
                <span class="font-mono text-[12.5px]">{{ scope }}</span>
                <span class="ml-2 text-ink-3">{{ t(`keys.scopeHints.${scope.replace(':', '_')}`) }}</span>
              </Checkbox>
            </div>
          </Field>
          <Field :label="t('keys.knowledgeBases')" :hint="t('keys.knowledgeBasesHint')">
            <p v-if="!bases.length" class="text-[12.5px] text-ink-3">{{ t("keys.noKnowledgeBases") }}</p>
            <div v-else class="grid max-h-48 gap-1.5 overflow-y-auto rounded-md border border-line p-2" data-test="key-kbs">
              <Checkbox v-for="kb in bases" :key="kb.id" :model-value="form.knowledge_base_ids.includes(kb.id)" @update:model-value="toggleKb(kb.id, $event)">
                {{ kb.name }}
              </Checkbox>
            </div>
          </Field>
          <div class="grid gap-4 sm:grid-cols-2">
            <Field :label="t('keys.rateLimit')" v-slot="{ id }">
              <Input :id="id" v-model="form.rate_limit_rpm" type="number" min="1" max="100000" data-test="key-rpm" :placeholder="t('keys.rateLimitDefault')" />
            </Field>
            <Field :label="t('keys.expires')" :hint="t('keys.expiresHint')" v-slot="{ id }">
              <Input :id="id" v-model="form.expires_at" type="datetime-local" />
            </Field>
          </div>
          <Field :label="t('keys.ipAllowlist')" :hint="t('keys.ipAllowlistHint')" v-slot="{ id }">
            <Input :id="id" v-model="form.ip_allowlist" placeholder="10.0.0.0/8, 192.168.1.10/32" data-test="key-cidr" />
          </Field>
          <button type="submit" class="hidden" aria-hidden="true" tabindex="-1" />
        </form>
      </template>
      <SecretReveal
        v-else
        v-model:acknowledged="acknowledged"
        :value="created.key"
        :label="t('keys.keyFor', { name: created.api_key.name })"
        :hint="t('keys.keyHint')"
      />
      <template #footer>
        <template v-if="!created">
          <Button variant="ghost" @click="createOpen = false">{{ t("common.cancel") }}</Button>
          <Button variant="primary" :disabled="!canSubmit" :loading="submitting" data-test="submit" @click="submit">{{ t("common.create") }}</Button>
        </template>
        <Button v-else variant="primary" :disabled="!acknowledged" data-test="done" @click="closeCreate">{{ t("common.done") }}</Button>
      </template>
    </Sheet>
  </div>
</template>
