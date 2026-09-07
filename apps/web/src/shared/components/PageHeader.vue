<script setup lang="ts">
defineProps<{
  title: string;
  subtitle?: string;
}>();
</script>

<template>
  <header class="page-header">
    <div>
      <h1>{{ title }}</h1>
      <p v-if="subtitle">{{ subtitle }}</p>
    </div>
    <div class="actions"><slot name="actions" /></div>
  </header>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--cairn-space-4);
  margin-bottom: var(--cairn-space-5);
}
h1 {
  margin: 0;
  font-size: 20px;
  letter-spacing: -0.01em;
}
p {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--cairn-text-muted);
  max-width: 62ch;
  line-height: 1.5;
}
.actions {
  display: flex;
  gap: var(--cairn-space-2);
  flex-shrink: 0;
}
</style>
