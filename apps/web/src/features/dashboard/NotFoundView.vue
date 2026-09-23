<script setup lang="ts">
import { useI18n } from "vue-i18n";

import BrandMark from "@/components/ui/BrandMark.vue";
import Button from "@/components/ui/Button.vue";
import ContourField from "@/components/ui/ContourField.vue";

const { t } = useI18n();
</script>

<template>
  <div class="relative flex min-h-screen flex-col items-center justify-center overflow-hidden bg-bg px-6 text-center">
    <ContourField :opacity="0.35" :animated="false" />
    <div class="relative grid justify-items-center gap-3">
      <BrandMark :size="36" />
      <p class="font-display text-[64px] font-semibold leading-none tracking-tight text-ink">404</p>
      <p class="max-w-sm text-[14px] text-ink-2">{{ t("notFound.detail") }}</p>
      <Button variant="primary" :to="{ name: 'dashboard' }" class="mt-2">{{ t("notFound.home") }}</Button>
    </div>
  </div>
</template>
