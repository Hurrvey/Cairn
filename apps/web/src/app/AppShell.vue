<script setup lang="ts">
/**
 * Authenticated application shell.
 *
 * Navigation is grouped by what the user is doing (working in knowledge
 * bases, connecting agents, setting up the deployment, administering the
 * workspace) and filtered by capability, so an operator only sees what their
 * role can reach. The server remains the authority on every request.
 */
import {
  Blocks,
  BookOpen,
  Cable,
  Compass,
  Database,
  KeyRound,
  LogOut,
  Menu,
  Moon,
  ScrollText,
  Search,
  Settings2,
  Sun,
  Users,
  WifiOff,
  ShieldCheck,
} from "lucide-vue-next";
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";

import { BRAND } from "@/app/brand";
import { setLocale } from "@/app/i18n";
import CommandPalette from "@/app/CommandPalette.vue";
import BrandMark from "@/components/ui/BrandMark.vue";
import Button from "@/components/ui/Button.vue";
import DropdownItem from "@/components/ui/DropdownItem.vue";
import DropdownMenu from "@/components/ui/DropdownMenu.vue";
import Kbd from "@/components/ui/Kbd.vue";
import Sheet from "@/components/ui/Sheet.vue";
import ChangePasswordDialog from "@/features/auth/ChangePasswordDialog.vue";
import ForcedCredentialDialog from "@/features/auth/ForcedCredentialDialog.vue";
import { cn } from "@/lib/utils";
import { useOnline } from "@/shared/composables/useOnline";
import { usePermissions } from "@/shared/composables/usePermissions";
import { useTheme } from "@/shared/composables/useTheme";
import { useSessionStore } from "@/stores/session";

const { t, locale } = useI18n();
const session = useSessionStore();
const permissions = usePermissions();
const router = useRouter();
const route = useRoute();
const theme = useTheme();
const online = useOnline();

const paletteOpen = ref(false);
const mobileNavOpen = ref(false);
const passwordOpen = ref(false);

interface NavItem {
  name: string;
  labelKey: string;
  icon: typeof Compass;
  visible: boolean;
}
interface NavGroup {
  key: string;
  items: NavItem[];
}

const groups = computed<NavGroup[]>(() =>
  [
    {
      key: "work",
      items: [
        { name: "dashboard", labelKey: "nav.dashboard", icon: Compass, visible: true },
        { name: "knowledge", labelKey: "nav.knowledge", icon: BookOpen, visible: true },
      ],
    },
    {
      key: "connect",
      items: [
        { name: "api-keys", labelKey: "nav.apiKeys", icon: KeyRound, visible: true },
        { name: "mcp-service", labelKey: "nav.mcpService", icon: Cable, visible: permissions.canManageSettings.value },
      ],
    },
    {
      key: "setup",
      items: [
        { name: "models", labelKey: "nav.models", icon: Blocks, visible: permissions.canManageModels.value },
        { name: "storage", labelKey: "nav.storage", icon: Database, visible: permissions.canManageStorage.value },
      ],
    },
    {
      key: "admin",
      items: [
        { name: "users", labelKey: "nav.users", icon: Users, visible: permissions.canManageUsers.value },
        { name: "audit", labelKey: "nav.audit", icon: ScrollText, visible: permissions.canReadAudit.value },
        { name: "settings", labelKey: "nav.settings", icon: Settings2, visible: permissions.canManageSettings.value },
      ],
    },
  ]
    .map((group) => ({ ...group, items: group.items.filter((item) => item.visible) }))
    .filter((group) => group.items.length > 0),
);

const activeName = computed(() => (route.name === "knowledge-detail" ? "knowledge" : String(route.name ?? "")));
const pageTitle = computed(() => (route.meta.titleKey ? t(route.meta.titleKey) : BRAND.name));
const offline = computed(() => !online.browserOnline.value || !online.serverReachable.value);
const initials = computed(() => {
  const name = session.user?.display_name || session.user?.username || "?";
  return name.slice(0, 2).toUpperCase();
});

watch(
  pageTitle,
  (title) => {
    document.title = `${title} · ${BRAND.name}`;
  },
  { immediate: true },
);

watch(
  () => route.fullPath,
  () => {
    mobileNavOpen.value = false;
  },
);

async function signOut(): Promise<void> {
  await session.signOut();
  await router.replace({ name: "login" });
}

function toggleLocale(): void {
  setLocale(locale.value === "en-US" ? "zh-CN" : "en-US");
}

function onKeydown(event: KeyboardEvent): void {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    paletteOpen.value = !paletteOpen.value;
  }
}
</script>

<template>
  <div class="min-h-screen bg-bg text-ink" @keydown="onKeydown">
    <!-- Sidebar -->
    <aside
      class="fixed inset-y-0 left-0 z-30 hidden w-[232px] flex-col border-r border-line bg-surface md:flex"
      data-test="sidebar"
    >
      <div class="flex h-12 items-center px-4">
        <RouterLink :to="{ name: 'dashboard' }" class="rounded-xs outline-none focus-visible:ring-2 focus-visible:ring-brand/40">
          <BrandMark :size="20" wordmark />
        </RouterLink>
      </div>
      <nav class="flex-1 overflow-y-auto px-2.5 pb-4" :aria-label="t('nav.primary')">
        <div v-for="group in groups" :key="group.key" class="mt-3 first:mt-1">
          <p class="mb-1 px-2 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-ink-3">
            {{ t(`nav.groups.${group.key}`) }}
          </p>
          <RouterLink
            v-for="item in group.items"
            :key="item.name"
            :to="{ name: item.name }"
            :data-test="`nav-${item.name}`"
            :aria-current="activeName === item.name ? 'page' : undefined"
            :class="
              cn(
                'group relative mb-0.5 flex h-8 items-center gap-2.5 rounded-sm px-2 text-[13px] text-ink-2 transition-colors outline-none focus-visible:ring-2 focus-visible:ring-brand/40',
                activeName === item.name ? 'bg-brand-soft/70 font-medium text-brand-strong' : 'hover:bg-surface-2 hover:text-ink',
              )
            "
          >
            <span
              v-if="activeName === item.name"
              class="absolute -left-2.5 top-1.5 h-5 w-0.5 rounded-r bg-brand"
              aria-hidden="true"
            />
            <component :is="item.icon" class="size-4 shrink-0" :class="activeName === item.name ? 'text-brand' : 'text-ink-3 group-hover:text-ink-2'" aria-hidden="true" />
            {{ t(item.labelKey) }}
          </RouterLink>
        </div>
      </nav>
      <div class="border-t border-line px-3 py-3">
        <button
          type="button"
          class="flex w-full items-center gap-2 rounded-sm px-1.5 py-1.5 text-left text-[12.5px] text-ink-2 hover:bg-surface-2"
          data-test="open-palette"
          @click="paletteOpen = true"
        >
          <Search class="size-3.5 text-ink-3" aria-hidden="true" />
          <span class="flex-1">{{ t("palette.trigger") }}</span>
          <Kbd>Ctrl</Kbd><Kbd>K</Kbd>
        </button>
      </div>
    </aside>

    <!-- Main column -->
    <div class="flex min-h-screen flex-col md:pl-[232px]">
      <header class="sticky top-0 z-20 flex h-12 items-center gap-2 border-b border-line bg-surface/85 px-3 backdrop-blur md:px-5">
        <Button variant="ghost" size="icon" class="md:hidden" :aria-label="t('nav.openMenu')" data-test="open-nav" @click="mobileNavOpen = true">
          <Menu aria-hidden="true" />
        </Button>
        <BrandMark :size="18" class="md:hidden" />
        <h2 class="min-w-0 flex-1 truncate font-sans text-[13px] font-medium text-ink-2" data-test="topbar-title">{{ pageTitle }}</h2>
        <Button variant="ghost" size="icon" class="md:hidden" :aria-label="t('palette.trigger')" @click="paletteOpen = true">
          <Search aria-hidden="true" />
        </Button>
        <Button variant="ghost" size="icon" :aria-label="t('shell.toggleTheme')" data-test="toggle-theme" @click="theme.toggle()">
          <Sun v-if="theme.resolved.value === 'dark'" aria-hidden="true" />
          <Moon v-else aria-hidden="true" />
        </Button>
        <Button variant="ghost" size="sm" data-test="toggle-locale" @click="toggleLocale">
          {{ locale === "en-US" ? "中文" : "EN" }}
        </Button>
        <DropdownMenu>
          <template #trigger>
            <button
              type="button"
              class="ml-1 flex h-8 items-center gap-2 rounded-full border border-line bg-surface pl-1 pr-2.5 text-[12.5px] text-ink hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-brand/40"
              data-test="user-menu"
            >
              <span class="flex size-6 items-center justify-center rounded-full bg-brand-soft font-display text-[11px] font-semibold text-brand-strong">{{ initials }}</span>
              <span class="hidden max-w-32 truncate sm:inline" data-test="current-user">{{ session.user?.username }}</span>
            </button>
          </template>
          <DropdownItem kind="label">
            {{ session.user?.display_name || session.user?.username }}
            <span v-if="permissions.isAdmin.value" class="ml-1 normal-case tracking-normal text-brand">· {{ t("dashboard.admin") }}</span>
          </DropdownItem>
          <DropdownItem @select="passwordOpen = true">
            <ShieldCheck aria-hidden="true" />
            {{ t("shell.changePassword") }}
          </DropdownItem>
          <DropdownItem kind="separator" />
          <DropdownItem data-test="sign-out" @select="signOut">
            <LogOut aria-hidden="true" />
            {{ t("dashboard.signOut") }}
          </DropdownItem>
        </DropdownMenu>
      </header>

      <div
        v-if="offline"
        class="flex items-center gap-2 border-b border-warn/30 bg-warn-soft px-4 py-1.5 text-[12.5px] text-warn"
        role="status"
        data-test="offline-banner"
      >
        <WifiOff class="size-3.5" aria-hidden="true" />
        {{ online.browserOnline.value ? t("shell.serverUnreachable") : t("shell.offline") }}
      </div>

      <main class="mx-auto w-full max-w-[1280px] flex-1 px-4 py-5 md:px-6 md:py-6">
        <RouterView v-slot="{ Component }">
          <Transition name="page" mode="out-in">
            <component :is="Component" :key="route.path" />
          </Transition>
        </RouterView>
      </main>
    </div>

    <!-- Mobile navigation -->
    <Sheet v-model:open="mobileNavOpen" side="left" size="sm" :title="BRAND.name" test-id="mobile-nav">
      <nav :aria-label="t('nav.primary')" class="-mx-2">
        <div v-for="group in groups" :key="group.key" class="mb-3">
          <p class="mb-1 px-2 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-ink-3">{{ t(`nav.groups.${group.key}`) }}</p>
          <RouterLink
            v-for="item in group.items"
            :key="item.name"
            :to="{ name: item.name }"
            :class="cn('flex h-9 items-center gap-2.5 rounded-sm px-2 text-[14px]', activeName === item.name ? 'bg-brand-soft/70 font-medium text-brand-strong' : 'text-ink-2')"
          >
            <component :is="item.icon" class="size-4" aria-hidden="true" />
            {{ t(item.labelKey) }}
          </RouterLink>
        </div>
      </nav>
    </Sheet>

    <CommandPalette v-model:open="paletteOpen" :groups="groups" @change-password="passwordOpen = true" />
    <ChangePasswordDialog v-model:open="passwordOpen" />
    <ForcedCredentialDialog />
  </div>
</template>
