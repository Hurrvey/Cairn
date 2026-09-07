<script setup lang="ts">
/**
 * Authenticated application shell.
 *
 * Navigation is filtered by capability, so an operator only sees what their
 * role can actually reach. Items for modules that do not exist yet are shown
 * disabled with their phase rather than hidden — the same honesty as
 * `capabilities` in GET /v1/meta, and it stops "where is the knowledge base
 * tab?" being a support question.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";

import ForcedCredentialDialog from "@/features/auth/ForcedCredentialDialog.vue";
import { usePermissions } from "@/shared/composables/usePermissions";
import { setLocale } from "@/app/i18n";
import { useSessionStore } from "@/stores/session";

const { t, locale } = useI18n();
const session = useSessionStore();
const permissions = usePermissions();
const router = useRouter();
const route = useRoute();

interface NavItem {
  name: string;
  labelKey: string;
  visible: boolean;
  phase?: number;
}

const nav = computed<NavItem[]>(() => [
  { name: "dashboard", labelKey: "nav.dashboard", visible: true },
  { name: "knowledge", labelKey: "nav.knowledge", visible: true, phase: 2 },
  { name: "users", labelKey: "nav.users", visible: permissions.canManageUsers.value },
  { name: "api-keys", labelKey: "nav.apiKeys", visible: true },
  { name: "audit", labelKey: "nav.audit", visible: permissions.canReadAudit.value },
  { name: "settings", labelKey: "nav.settings", visible: permissions.canManageSettings.value },
]);

async function signOut(): Promise<void> {
  await session.signOut();
  await router.replace({ name: "login" });
}

function toggleLocale(): void {
  setLocale(locale.value === "en-US" ? "zh-CN" : "en-US");
}
</script>

<template>
  <div class="shell">
    <aside>
      <div class="brand">
        <span class="mark" aria-hidden="true">◭</span>
        <strong>Cairn</strong>
      </div>

      <nav>
        <template v-for="item in nav" :key="item.name">
          <RouterLink
            v-if="item.visible && !item.phase"
            :to="{ name: item.name }"
            :class="{ active: route.name === item.name }"
            :data-test="`nav-${item.name}`"
          >
            {{ t(item.labelKey) }}
          </RouterLink>
          <span v-else-if="item.visible" class="pending" :data-test="`nav-${item.name}`">
            {{ t(item.labelKey) }}
            <em>{{ t("nav.phase", { n: item.phase }) }}</em>
          </span>
        </template>
      </nav>

      <div class="foot">
        <button type="button" class="linkish" data-test="toggle-locale" @click="toggleLocale">
          {{ locale === "en-US" ? "中文" : "English" }}
        </button>
      </div>
    </aside>

    <div class="main">
      <header>
        <span class="spacer" />
        <span class="who" data-test="current-user">{{ session.user?.username }}</span>
        <el-tag v-if="permissions.isAdmin.value" size="small" type="warning">
          {{ t("dashboard.admin") }}
        </el-tag>
        <el-button link data-test="sign-out" @click="signOut">
          {{ t("dashboard.signOut") }}
        </el-button>
      </header>

      <main><RouterView /></main>
    </div>

    <ForcedCredentialDialog />
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-columns: 216px 1fr;
  min-height: 100vh;
  background: var(--cairn-surface-sunken);
}
aside {
  display: flex;
  flex-direction: column;
  background: var(--cairn-surface);
  border-right: 1px solid var(--cairn-border);
  padding: var(--cairn-space-4);
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px var(--cairn-space-5);
}
.brand .mark {
  color: var(--cairn-accent);
  font-size: 18px;
}
nav {
  display: grid;
  gap: 2px;
}
nav a,
nav .pending {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--cairn-text);
  text-decoration: none;
}
nav a:hover {
  background: var(--cairn-surface-sunken);
}
nav a.active {
  background: var(--cairn-surface-sunken);
  font-weight: 600;
  color: var(--cairn-accent-strong);
}
nav .pending {
  color: var(--cairn-text-muted);
  cursor: default;
}
nav .pending em {
  font-style: normal;
  font-size: 11px;
  opacity: 0.75;
}
.foot {
  margin-top: auto;
  padding-top: var(--cairn-space-4);
}
.linkish {
  background: none;
  border: 0;
  padding: 4px 10px;
  color: var(--cairn-text-muted);
  font-size: 12px;
  cursor: pointer;
}
.main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
header {
  display: flex;
  align-items: center;
  gap: var(--cairn-space-3);
  height: 52px;
  padding: 0 var(--cairn-space-6);
  background: var(--cairn-surface);
  border-bottom: 1px solid var(--cairn-border);
}
.spacer {
  flex: 1;
}
.who {
  font-size: 13px;
  color: var(--cairn-text-muted);
}
main {
  padding: var(--cairn-space-6);
  max-width: 1180px;
  width: 100%;
}
</style>
