<script setup lang="ts">
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";

import { usePermissions } from "@/shared/composables/usePermissions";
import { useSessionStore } from "@/stores/session";

const { t } = useI18n();
const session = useSessionStore();
const permissions = usePermissions();
const router = useRouter();

// Modules are listed with their phase rather than hidden, so an operator can
// see what this deployment does and does not have — the same honesty as
// `capabilities` in GET /v1/meta.
const modules = [
  { key: "knowledge", phase: 2, ready: false, route: null },
  { key: "ingestion", phase: 2, ready: false, route: null },
  { key: "retrieval", phase: 2, ready: false, route: null },
  { key: "mcp", phase: 3, ready: false, route: null },
  { key: "models", phase: 3, ready: false, route: null },
  { key: "pipelines", phase: 4, ready: false, route: null },
];

const shortcuts = [
  { key: "users", route: "users", visible: permissions.canManageUsers.value },
  { key: "apiKeys", route: "api-keys", visible: true },
  { key: "audit", route: "audit", visible: permissions.canReadAudit.value },
  { key: "settings", route: "settings", visible: permissions.canManageSettings.value },
];
</script>

<template>
  <div>
    <h1>
      {{ t("dashboard.welcome", { name: session.user?.display_name || session.user?.username }) }}
    </h1>
    <p class="lede">{{ t("dashboard.phaseNotice") }}</p>

    <h2>{{ t("dashboard.available") }}</h2>
    <div class="grid">
      <button
        v-for="item in shortcuts.filter((s) => s.visible)"
        :key="item.key"
        type="button"
        class="card ready"
        :data-test="`shortcut-${item.key}`"
        @click="router.push({ name: item.route })"
      >
        <h3>{{ t(`nav.${item.key}`) }}</h3>
        <p>{{ t(`dashboard.shortcuts.${item.key}`) }}</p>
      </button>
    </div>

    <h2>{{ t("dashboard.comingLater") }}</h2>
    <div class="grid">
      <article v-for="module in modules" :key="module.key" class="card">
        <h3>{{ t(`dashboard.modules.${module.key}.title`) }}</h3>
        <p>{{ t(`dashboard.modules.${module.key}.detail`) }}</p>
        <el-tag size="small" type="info">{{ t("dashboard.phase", { n: module.phase }) }}</el-tag>
      </article>
    </div>
  </div>
</template>

<style scoped>
h1 {
  margin: 0 0 6px;
  font-size: 22px;
  letter-spacing: -0.01em;
}
.lede {
  margin: 0 0 var(--cairn-space-6);
  color: var(--cairn-text-muted);
}
h2 {
  margin: 0 0 var(--cairn-space-3);
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--cairn-text-muted);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--cairn-space-4);
  margin-bottom: var(--cairn-space-6);
}
.card {
  display: block;
  width: 100%;
  text-align: left;
  background: var(--cairn-surface);
  border: 1px solid var(--cairn-border);
  border-radius: 8px;
  padding: var(--cairn-space-4);
  opacity: 0.72;
  font: inherit;
  color: inherit;
}
.card.ready {
  opacity: 1;
  cursor: pointer;
}
.card.ready:hover {
  border-color: var(--cairn-accent);
}
h3 {
  margin: 0 0 4px;
  font-size: 14px;
}
.card p {
  margin: 0 0 var(--cairn-space-3);
  font-size: 13px;
  color: var(--cairn-text-muted);
  line-height: 1.5;
}
</style>
