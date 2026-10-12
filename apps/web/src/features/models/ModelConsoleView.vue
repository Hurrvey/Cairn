<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { api } from "@/api/client";
import type { Model } from "@/api/setup";
import Button from "@/components/ui/Button.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import Notice from "@/components/ui/Notice.vue";
import PageHeader from "@/shared/components/PageHeader.vue";
import ChatConsole from "./ChatConsole.vue";

const { t } = useI18n();
const models = ref<Model[]>([]);
const selected = ref<Model | null>(null);
const open = ref(false);
const loading = ref(true), failed = ref(false);
async function load() {
  loading.value = true;
  try { models.value = await api.get<Model[]>("/v1/models/usable"); failed.value = false; }
  catch { failed.value = true; }
  finally { loading.value = false; }
}
onMounted(load);
</script>
<template>
  <div>
    <PageHeader :title="t('nav.modelConsole')" :description="t('models.chat.hint')"><template #actions><Button @click="load">{{ t('common.refresh') }}</Button></template></PageHeader>
    <Notice v-if="failed" tone="bad">{{ t('models.chat.failed') }}</Notice>
    <p v-else-if="loading" role="status">{{ t('models.chat.loading') }}</p>
    <EmptyState v-else-if="!models.length" :title="t('models.chat.noModels')" :description="t('models.chat.requestAccess')" />
    <ul v-else class="divide-y divide-line rounded-md border border-line">
      <li v-for="model in models" :key="model.id" class="flex items-center justify-between p-3"><div><p>{{ model.display_name }}</p><p class="text-xs text-ink-3">{{ model.provider_family }} · {{ model.model_key }}</p></div><Button @click="selected = model; open = true">{{ t('models.chat.open') }}</Button></li>
    </ul>
    <ChatConsole v-model:open="open" :model="selected" />
  </div>
</template>
