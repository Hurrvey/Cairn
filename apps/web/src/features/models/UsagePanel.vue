<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { api } from "@/api/client";
import Button from "@/components/ui/Button.vue";
import Notice from "@/components/ui/Notice.vue";

interface Summary { events: number; input_tokens: number | null; output_tokens: number | null; unknown_cost_events: number; unknown_input_events: number; unknown_output_events: number; cost_coverage: number | null; estimated_events: number; costs: Record<string, string>; }
const summary = ref<Summary | null>(null);
const failed = ref(false);
const { t } = useI18n();
async function load() { try { summary.value = await api.get<Summary>("/v1/usage"); failed.value = false; } catch { failed.value = true; } }
onMounted(load);
</script>
<template>
  <section class="mt-6 border-t border-line pt-4" data-test="usage-panel">
    <div class="flex items-center justify-between"><h2 class="text-sm font-semibold">{{ t('models.usage.title') }}</h2><Button variant="ghost" size="sm" @click="load">{{ t('common.refresh') }}</Button></div>
    <Notice v-if="failed" tone="bad">{{ t('models.usage.failed') }}</Notice>
    <p class="mt-1 text-xs text-ink-3">{{ t('models.usage.hint') }}</p>
    <dl v-if="summary" class="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
      <div><dt class="text-ink-3">{{ t('models.usage.calls') }}</dt><dd>{{ summary.events ?? 0 }}</dd></div>
      <div><dt class="text-ink-3">{{ t('models.usage.input') }}</dt><dd>{{ summary.input_tokens ?? '—' }}</dd></div>
      <div><dt class="text-ink-3">{{ t('models.usage.output') }}</dt><dd>{{ summary.output_tokens ?? '—' }}</dd></div>
      <div><dt class="text-ink-3">{{ t('models.usage.cost') }}</dt><dd v-for="(cost, currency) in summary.costs" :key="currency">{{ currency }} {{ cost }}</dd><dd v-if="!summary.costs || !Object.keys(summary.costs).length">—</dd></div>
    </dl>
    <p v-if="summary" class="mt-2 text-xs text-ink-3">{{ t('models.usage.unknown', { n: summary.unknown_cost_events ?? 0 }) }}</p>
    <p v-if="summary" class="mt-1 text-xs text-ink-3">{{ t('models.usage.coverage', { input: summary.unknown_input_events ?? 0, output: summary.unknown_output_events ?? 0, coverage: summary.cost_coverage == null ? '—' : `${Math.round(summary.cost_coverage * 100)}%` }) }}</p>
  </section>
</template>
