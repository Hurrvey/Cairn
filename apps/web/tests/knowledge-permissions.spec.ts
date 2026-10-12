import { expect, it } from "vitest";
import { allowsKnowledgeAction } from "@/shared/composables/useKnowledgePermissions";

it("limits affordances to the effective resource and implied permissions", () => {
  const permissions = {
    workspace_permissions: ["kb:create"],
    resource_permissions: { kb_notes: ["kb:read"] },
  };
  expect(allowsKnowledgeAction(permissions, "kb:read", "kb_notes")).toBe(true);
  expect(allowsKnowledgeAction(permissions, "kb:query", "kb_notes")).toBe(true);
  expect(allowsKnowledgeAction(permissions, "kb:write", "kb_notes")).toBe(
    false,
  );
  expect(allowsKnowledgeAction(permissions, "kb:read", "kb_other")).toBe(false);
  expect(allowsKnowledgeAction(permissions, "kb:create")).toBe(true);
  expect(allowsKnowledgeAction(undefined, "kb:manage", "kb_notes")).toBe(false);
});
