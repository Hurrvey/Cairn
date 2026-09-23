<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";

import Button from "@/components/ui/Button.vue";
import Dialog from "@/components/ui/Dialog.vue";
import { useConfirmState } from "@/shared/composables/useConfirm";

const { t } = useI18n();
const { state, settle } = useConfirmState();

const open = computed({
  get: () => state.current !== null,
  set: (value: boolean) => {
    if (!value) settle(false);
  },
});
</script>

<template>
  <Dialog
    v-model:open="open"
    :title="state.current?.title ?? ''"
    :description="state.current?.description"
    size="sm"
    test-id="confirm-dialog"
  >
    <template #footer>
      <Button variant="ghost" data-test="confirm-cancel" @click="settle(false)">
        {{ state.current?.cancelLabel ?? t("common.cancel") }}
      </Button>
      <Button :variant="state.current?.danger ? 'danger' : 'primary'" data-test="confirm-accept" @click="settle(true)">
        {{ state.current?.confirmLabel ?? t("common.confirm") }}
      </Button>
    </template>
  </Dialog>
</template>
