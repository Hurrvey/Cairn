<script setup lang="ts">
/**
 * Explain output (FR-H-08): the plan the server ran and what each stage did to
 * the ranking. Answers "was it never indexed, ranked 400th, cut by the
 * threshold, or dropped by the budget?" without reading logs.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import type { ExplainOutput } from "@/api/knowledge";
import Badge from "@/components/ui/Badge.vue";

type Stage = Record<string, unknown> & { stage: string; latency_ms?: number };

const props = defineProps<{ explain: ExplainOutput }>();
const { t } = useI18n();

const stages = computed(() => (props.explain.stages ?? []) as Stage[]);
const plan = computed(() => props.explain.plan ?? {});
const planJson = computed(() => JSON.stringify(plan.value, null, 2));

function count(stage: Stage, key: string): number | null {
  const value = stage[key];
  return typeof value === "number" ? value : null;
}

function summary(stage: Stage): string {
  const parts: string[] = [];
  const returned = count(stage, "returned");
  if (returned !== null) parts.push(t("knowledge.search.explainReturned", { n: returned }));
  for (const key of ["removed", "removed_by_threshold", "dropped"]) {
    const removed = count(stage, key);
    if (removed) parts.push(t("knowledge.search.explainRemoved", { n: removed }));
  }
  const collapsed = count(stage, "children_collapsed");
  if (collapsed) {
    parts.push(t("knowledge.search.explainCollapsed", { n: collapsed, parents: count(stage, "parents_returned") ?? 0 }));
  }
  const changes = stage.rank_changes;
  if (Array.isArray(changes) && changes.length) parts.push(t("knowledge.search.explainMoved", { n: changes.length }));
  for (const key of ["method", "mode", "model"]) {
    if (typeof stage[key] === "string") parts.push(String(stage[key]));
  }
  return parts.join(" · ");
}

function rows(stage: Stage): Record<string, unknown>[] {
  const list = Array.isArray(stage.rank_changes) && stage.rank_changes.length ? stage.rank_changes : stage.top;
  return Array.isArray(list) ? (list as Record<string, unknown>[]) : [];
}

function cell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(4);
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function columns(list: Record<string, unknown>[]): string[] {
  const keys = new Set<string>();
  for (const row of list) for (const key of Object.keys(row)) keys.add(key);
  return [...keys];
}
</script>

<template>
  <section class="grid gap-3 rounded-md border border-line bg-surface-2 p-3" data-test="explain-panel">
    <h3 class="font-display text-[13.5px] font-semibold text-ink">{{ t("knowledge.search.explainTitle") }}</h3>
    <details class="text-[12.5px]">
      <summary class="cursor-pointer text-ink-2">{{ t("knowledge.search.explainPlan") }}</summary>
      <pre class="mt-2 max-h-72 overflow-auto rounded border border-line bg-surface p-2 font-mono text-[11.5px] text-ink-2" data-test="explain-plan">{{ planJson }}</pre>
    </details>
    <table class="w-full border-collapse text-left text-[12.5px]">
      <thead class="text-ink-3">
        <tr>
          <th class="py-1 pr-3 font-medium">{{ t("knowledge.search.explainStage") }}</th>
          <th class="py-1 pr-3 font-medium">{{ t("knowledge.search.explainTime") }}</th>
          <th class="py-1 font-medium">{{ t("knowledge.search.explainDetail") }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(stage, index) in stages" :key="`${stage.stage}-${index}`" class="border-t border-line align-top" data-test="explain-stage">
            <td class="py-1.5 pr-3">
              <Badge size="sm" tone="neutral">{{ stage.stage }}</Badge>
            </td>
            <td class="tnum py-1.5 pr-3 text-ink-2">{{ stage.latency_ms ?? 0 }} ms</td>
            <td class="py-1.5 text-ink-2">
              <span>{{ summary(stage) }}</span>
              <details v-if="rows(stage).length" class="mt-1">
                <summary class="cursor-pointer text-ink-3">{{ rows(stage).length }}</summary>
                <div class="mt-1 overflow-x-auto">
                  <table class="border-collapse font-mono text-[11px]">
                    <thead>
                      <tr>
                        <th v-for="column in columns(rows(stage))" :key="column" class="pr-3 text-left font-medium text-ink-3">{{ column }}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(row, rowIndex) in rows(stage)" :key="rowIndex">
                        <td v-for="column in columns(rows(stage))" :key="column" class="max-w-[40ch] truncate pr-3" :title="cell(row[column])">{{ cell(row[column]) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </details>
            </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
