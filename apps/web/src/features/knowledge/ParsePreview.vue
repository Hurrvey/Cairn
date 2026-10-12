<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from "vue";
import { useI18n } from "vue-i18n";
import { api, ApiError } from "@/api/client";
import { describeError } from "@/shared/errors";

interface Location { page: number; width: number; height: number; bbox: number[]; crop: number[] | null; rotation: number }
interface Cell { text: string; row_start: number; row_end: number; col_start: number; col_end: number }
interface Element { element_id: string; type: string; text: string; locations: Location[];
  table: { rows: number; columns: number; cells: Cell[] } | null;
  formula: { native: string; recognized: string | null; status: string } | null }
interface Summary { parse_version: string; revision: number; pages: number; engine: string; warnings: string[]; quality_status: string }
const props = defineProps<{ documentId: string }>();
const { t } = useI18n();
const summary = ref<Summary | null>(null);
const elements = ref<Element[]>([]);
const page = ref(1);
const view = ref("active");
const error = ref("");
const legacyText = ref("");
const cursor = ref<string | null>(null);
const selected = ref<Element | null>(null);
const previewError = ref(false);
let controller = new AbortController();
let generation = 0;
const root = computed(() => `/v1/documents/${encodeURIComponent(props.documentId)}/parse`);
const query = computed(() => `view=${view.value}&parse_version=${summary.value?.parse_version}`);
const preview = computed(() => `${root.value}/pages/${page.value}/preview?${query.value}`);

async function load(more = false): Promise<void> {
  const current = ++generation;
  controller.abort();
  controller = new AbortController();
  error.value = "";
  legacyText.value = "";
  try {
    if (!more) {
      const next = await api.get<Summary>(`${root.value}?view=${view.value}`, { signal: controller.signal });
      if (current !== generation) return;
      summary.value = next;
    }
    const data = await api.get<{ items: Element[]; next_cursor: string | null }>(
      `${root.value}/elements?${query.value}&page=${page.value}&limit=200${more && cursor.value ? `&cursor=${encodeURIComponent(cursor.value)}` : ""}`,
      { signal: controller.signal },
    );
    if (current !== generation) return;
    elements.value = more ? [...elements.value, ...data.items] : data.items;
    cursor.value = data.next_cursor;
    selected.value = elements.value[0] ?? null;
    previewError.value = false;
  } catch (caught) {
    if (current !== generation || controller.signal.aborted) return;
    error.value = describeError(caught).message; summary.value = null;
    if (caught instanceof ApiError && caught.code === "PARSE_LEGACY_UNAVAILABLE") {
      try {
        const response = await fetch(`/v1/documents/${encodeURIComponent(props.documentId)}/content`, { signal: controller.signal });
        if (response.ok && current === generation) legacyText.value = await response.text();
      } catch { /* A changed selection aborts this legacy read. */ }
    }
  }
}

function rectangle(location: Location): Record<string, string> {
  const crop = location.crop ?? [0, 0, location.width, location.height];
  const w = crop[2]! - crop[0]!, h = crop[3]! - crop[1]!;
  const points = [[Math.max(crop[0]!, location.bbox[0]!), Math.max(crop[1]!, location.bbox[1]!)], [Math.min(crop[2]!, location.bbox[2]!), Math.min(crop[3]!, location.bbox[3]!)]];
  const rotated = points.map(([x, y]) => {
    const nx = (x! - crop[0]!) / w, ny = (y! - crop[1]!) / h;
    switch (location.rotation) {
      case 90: return [ny, nx];
      case 180: return [1 - nx, ny];
      case 270: return [1 - ny, 1 - nx];
      default: return [nx, 1 - ny];
    }
  });
  const xs = rotated.map(p => p[0]!), ys = rotated.map(p => p[1]!);
  return { left: `${100 * Math.min(...xs)}%`, top: `${100 * Math.min(...ys)}%`,
    width: `${100 * (Math.max(...xs) - Math.min(...xs))}%`, height: `${100 * (Math.max(...ys) - Math.min(...ys))}%` };
}
function intersects(location: Location): boolean {
  const crop = location.crop ?? [0, 0, location.width, location.height];
  return Math.max(crop[0]!, location.bbox[0]!) < Math.min(crop[2]!, location.bbox[2]!) &&
    Math.max(crop[1]!, location.bbox[1]!) < Math.min(crop[3]!, location.bbox[3]!);
}
watch([() => props.documentId, view, page], () => void load(), { immediate: true });
onBeforeUnmount(() => { generation++; controller.abort(); });
</script>

<template>
  <section class="border-t border-line p-4" data-test="parse-preview">
    <div class="mb-3 flex items-center gap-3">
      <h3 class="text-sm font-semibold">{{ t("knowledge.parse.title") }}</h3>
      <select v-model="view" class="rounded border border-line bg-surface p-1 text-xs">
        <option value="active">{{ t("knowledge.parse.active") }}</option>
        <option value="candidate">{{ t("knowledge.parse.candidate") }}</option>
      </select>
      <input v-model.number="page" type="number" min="1" :max="summary?.pages ?? 1" class="w-16 rounded border border-line p-1 text-xs" :aria-label="t('knowledge.parse.page')" />
    </div>
    <p v-if="error" class="text-xs text-ink-3">{{ error }}</p>
    <pre v-if="legacyText" class="mt-2 max-h-96 overflow-auto whitespace-pre-wrap text-xs">{{ legacyText }}</pre>
    <template v-if="summary">
      <p class="mb-2 break-all font-mono text-xs">{{ summary.engine }} · r{{ summary.revision }} · {{ summary.parse_version }}</p>
      <p class="mb-2 text-xs text-ink-3">{{ t("knowledge.parse.unscored") }}</p>
      <p v-for="warning in summary.warnings" :key="warning" class="mb-1 text-xs text-ink-3">{{ warning }}</p>
      <div class="grid gap-4 md:grid-cols-2">
        <div>
          <p v-if="previewError" class="text-xs text-ink-3">{{ t("knowledge.parse.previewUnavailable") }}</p>
          <div v-else class="relative inline-block w-full">
            <img :src="preview" class="w-full" :alt="t('knowledge.parse.page')" @error="previewError = true" />
            <template v-if="selected">
              <div v-for="(location, index) in selected.locations.filter(l => l.page === page && intersects(l))" :key="index" class="pointer-events-none absolute border-2 border-blue-500 bg-blue-500/10" :style="rectangle(location)" />
            </template>
          </div>
        </div>
        <div class="max-h-[600px] overflow-auto">
          <button v-for="element in elements" :key="element.element_id" class="mb-2 block w-full rounded border border-line p-2 text-left text-xs" @click="selected = element">
            <span class="font-semibold">{{ element.type }}</span>
            <pre class="mt-1 whitespace-pre-wrap font-sans">{{ element.formula?.native ?? element.text }}</pre>
            <p v-if="element.formula">{{ element.formula.status }} {{ element.formula.recognized }}</p>
            <table v-if="element.table" class="mt-2 w-full border-collapse">
              <tbody><tr v-for="row in element.table.rows" :key="row">
                <td v-for="cell in element.table.cells.filter(c => c.row_start === row - 1)" :key="cell.col_start" :rowspan="cell.row_end - cell.row_start" :colspan="cell.col_end - cell.col_start" class="border border-line p-1">{{ cell.text }}</td>
              </tr></tbody>
            </table>
          </button>
          <button v-if="cursor" class="text-xs underline" @click="load(true)">{{ t("common.loadMore") }}</button>
        </div>
      </div>
    </template>
  </section>
</template>
