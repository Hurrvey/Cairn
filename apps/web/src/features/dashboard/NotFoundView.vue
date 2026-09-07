<script setup lang="ts">
import { useI18n } from "vue-i18n";

const { t } = useI18n();
</script>

<template>
  <div class="not-found">
    <h1>404</h1>
    <p>{{ t("notFound.detail") }}</p>
    <RouterLink to="/">{{ t("notFound.home") }}</RouterLink>
  </div>
</template>

<style scoped>
.not-found {
  min-height: 100vh;
  display: grid;
  place-content: center;
  text-align: center;
  gap: 8px;
  color: var(--cairn-text-muted);
}
h1 {
  margin: 0;
  font-size: 48px;
  color: var(--cairn-text);
}
</style>
