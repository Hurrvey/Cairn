import { onBeforeUnmount, onMounted, ref } from "vue";
import { api } from "@/api/client";
import { useSessionStore } from "@/stores/session";

interface KnowledgePermissions {
  workspace_permissions: string[];
  resource_permissions: Record<string, string[]>;
}
const implied: Record<string, string[]> = {
  "kb:query": ["kb:query", "kb:read", "kb:write", "kb:manage"],
  "kb:read": ["kb:read", "kb:write", "kb:manage"],
  "kb:write": ["kb:write", "kb:manage"],
};

export function allowsKnowledgeAction(
  permissions: KnowledgePermissions | undefined,
  action: string,
  id?: string,
): boolean {
  if (!permissions) return false;
  const held = [
    ...permissions.workspace_permissions,
    ...(id ? (permissions.resource_permissions[id] ?? []) : []),
  ];
  return (implied[action] ?? [action]).some((permission) =>
    held.includes(permission),
  );
}

export function useKnowledgePermissions() {
  const session = useSessionStore();
  const permissions = ref<KnowledgePermissions>();
  const controller = new AbortController();
  onMounted(async () => {
    if (!session.user) return;
    try {
      permissions.value = await api.get<KnowledgePermissions>(
        `/v1/users/${session.user.id}/permissions`,
        { signal: controller.signal },
      );
    } catch {
      permissions.value = undefined;
    }
  });
  onBeforeUnmount(() => controller.abort());
  return {
    can: (action: string, id?: string) =>
      allowsKnowledgeAction(permissions.value, action, id),
  };
}
