<script setup lang="ts">
/**
 * Command palette: jump to a page or a knowledge base, or run a common action,
 * without leaving the keyboard. Knowledge bases are fetched when the palette
 * opens so the list is never stale.
 */
import { BookOpen, CornerDownLeft, Languages, Moon, Plus, ShieldCheck, Sun } from "lucide-vue-next";
import { DialogContent, DialogOverlay, DialogPortal, DialogRoot, DialogTitle, VisuallyHidden } from "reka-ui";
import { computed, nextTick, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";

import { knowledge, type KnowledgeBase } from "@/api/knowledge";
import { setLocale } from "@/app/i18n";
import Kbd from "@/components/ui/Kbd.vue";
import { cn } from "@/lib/utils";
import { useTheme } from "@/shared/composables/useTheme";

interface NavGroup {
  key: string;
  items: { name: string; labelKey: string; icon: unknown; visible: boolean }[];
}

interface Command {
  id: string;
  group: "pages" | "knowledge" | "actions";
  label: string;
  hint?: string;
  icon: unknown;
  run: () => void | Promise<unknown>;
}

const props = defineProps<{ groups: NavGroup[] }>();
const emit = defineEmits<{ changePassword: [] }>();
const open = defineModel<boolean>("open", { default: false });

const { t, locale } = useI18n();
const router = useRouter();
const theme = useTheme();

const query = ref("");
const active = ref(0);
const input = ref<HTMLInputElement>();
const bases = ref<KnowledgeBase[]>([]);
const loadingBases = ref(false);

const commands = computed<Command[]>(() => {
  const pages: Command[] = props.groups.flatMap((group) =>
    group.items.map((item) => ({
      id: `page:${item.name}`,
      group: "pages" as const,
      label: t(item.labelKey),
      hint: t(`nav.groups.${group.key}`),
      icon: item.icon,
      run: () => router.push({ name: item.name }),
    })),
  );
  const kbs: Command[] = bases.value.map((kb) => ({
    id: `kb:${kb.id}`,
    group: "knowledge" as const,
    label: kb.name,
    hint: kb.description ?? undefined,
    icon: BookOpen,
    run: () => router.push({ name: "knowledge-detail", params: { id: kb.id } }),
  }));
  const actions: Command[] = [
    {
      id: "action:new-kb",
      group: "actions",
      label: t("palette.newKnowledgeBase"),
      icon: Plus,
      run: () => router.push({ name: "knowledge", query: { create: "1" } }),
    },
    {
      id: "action:theme",
      group: "actions",
      label: theme.resolved.value === "dark" ? t("palette.lightTheme") : t("palette.darkTheme"),
      icon: theme.resolved.value === "dark" ? Sun : Moon,
      run: () => theme.toggle(),
    },
    {
      id: "action:locale",
      group: "actions",
      label: locale.value === "en-US" ? "切换到中文" : "Switch to English",
      icon: Languages,
      run: () => setLocale(locale.value === "en-US" ? "zh-CN" : "en-US"),
    },
    {
      id: "action:password",
      group: "actions",
      label: t("shell.changePassword"),
      icon: ShieldCheck,
      run: () => emit("changePassword"),
    },
  ];
  return [...pages, ...kbs, ...actions];
});

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase();
  if (!needle) return commands.value;
  return commands.value.filter(
    (command) =>
      command.label.toLowerCase().includes(needle) || (command.hint ?? "").toLowerCase().includes(needle),
  );
});

const grouped = computed(() => {
  const order: Command["group"][] = ["knowledge", "pages", "actions"];
  return order
    .map((group) => ({ group, items: filtered.value.filter((command) => command.group === group) }))
    .filter((entry) => entry.items.length > 0);
});

const flat = computed(() => grouped.value.flatMap((entry) => entry.items));

watch(open, async (value) => {
  if (!value) return;
  query.value = "";
  active.value = 0;
  await nextTick();
  input.value?.focus();
  loadingBases.value = true;
  try {
    bases.value = (await knowledge.list()).items;
  } catch {
    bases.value = [];
  } finally {
    loadingBases.value = false;
  }
});

watch(filtered, () => {
  active.value = 0;
});

async function run(command: Command): Promise<void> {
  open.value = false;
  await command.run();
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "ArrowDown") {
    event.preventDefault();
    active.value = Math.min(active.value + 1, flat.value.length - 1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    active.value = Math.max(active.value - 1, 0);
  } else if (event.key === "Enter") {
    const command = flat.value[active.value];
    if (command) void run(command);
  }
}

function indexOf(command: Command): number {
  return flat.value.indexOf(command);
}
</script>

<template>
  <DialogRoot v-model:open="open">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-50 bg-overlay data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 duration-100" />
      <DialogContent
        class="fixed left-1/2 top-[12vh] z-50 w-[calc(100vw-2rem)] max-w-[560px] -translate-x-1/2 overflow-hidden rounded-lg border border-line bg-surface shadow-pop outline-none data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-[0.98] data-[state=closed]:animate-out data-[state=closed]:fade-out-0 duration-100"
        data-test="command-palette"
        @keydown="onKeydown"
      >
        <VisuallyHidden><DialogTitle>{{ t("palette.trigger") }}</DialogTitle></VisuallyHidden>
        <div class="flex items-center gap-2 border-b border-line px-3">
          <input
            ref="input"
            v-model="query"
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            :placeholder="t('palette.placeholder')"
            class="h-11 flex-1 bg-transparent text-[14px] outline-none placeholder:text-ink-3"
            data-test="palette-input"
          />
          <Kbd>Esc</Kbd>
        </div>
        <div id="palette-list" role="listbox" class="max-h-[50vh] overflow-y-auto p-1.5">
          <p v-if="!flat.length" class="px-3 py-6 text-center text-[13px] text-ink-3">{{ t("palette.empty") }}</p>
          <template v-for="entry in grouped" :key="entry.group">
            <p class="px-2 pb-1 pt-2 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-ink-3">
              {{ t(`palette.groups.${entry.group}`) }}
            </p>
            <button
              v-for="command in entry.items"
              :key="command.id"
              type="button"
              role="option"
              :aria-selected="indexOf(command) === active"
              :class="
                cn(
                  'flex w-full items-center gap-2.5 rounded-sm px-2 py-1.5 text-left text-[13px] text-ink outline-none',
                  indexOf(command) === active ? 'bg-brand-soft/70 text-brand-strong' : 'hover:bg-surface-2',
                )
              "
              :data-test="`palette-${command.id}`"
              @mouseenter="active = indexOf(command)"
              @click="run(command)"
            >
              <component :is="command.icon" class="size-4 shrink-0 text-ink-3" aria-hidden="true" />
              <span class="min-w-0 flex-1 truncate">{{ command.label }}</span>
              <span v-if="command.hint" class="max-w-40 truncate text-[11.5px] text-ink-3">{{ command.hint }}</span>
              <CornerDownLeft v-if="indexOf(command) === active" class="size-3.5 shrink-0 text-ink-3" aria-hidden="true" />
            </button>
          </template>
        </div>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
