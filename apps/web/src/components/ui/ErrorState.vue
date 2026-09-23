<script setup lang="ts">
import { AlertTriangle, RefreshCw } from "lucide-vue-next";
import { useI18n } from "vue-i18n";

import Button from "@/components/ui/Button.vue";
import { cn } from "@/lib/utils";

// Boolean props default to false when absent, so the retry affordance is opt-out.
withDefaults(defineProps<{ message: string; requestId?: string | undefined; compact?: boolean; class?: string; retryable?: boolean }>(), { retryable: true });
const emit = defineEmits<{ retry: [] }>();
const { t } = useI18n();
</script>

<template>
  <div
    role="alert"
    :class="
      cn(
        'flex items-start gap-3 rounded-md border border-bad/30 bg-bad-soft text-[13px] text-ink',
        compact ? 'px-3 py-2' : 'px-4 py-3',
        $props.class,
      )
    "
    data-test="error-state"
  >
    <AlertTriangle class="mt-0.5 size-4 shrink-0 text-bad" aria-hidden="true" />
    <div class="min-w-0 flex-1">
      <p class="leading-snug">{{ message }}</p>
      <p v-if="requestId" class="mt-1 font-mono text-[11px] text-ink-3">{{ t("errors.requestId") }} {{ requestId }}</p>
    </div>
    <Button v-if="retryable" size="sm" variant="outline" class="shrink-0" data-test="retry" @click="emit('retry')">
      <RefreshCw aria-hidden="true" />
      {{ t("common.retry") }}
    </Button>
  </div>
</template>
