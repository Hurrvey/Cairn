import { flushPromises } from "@vue/test-utils";
import { expect, it } from "vitest";

import ForcedCredentialDialog from "@/features/auth/ForcedCredentialDialog.vue";
import LoginView from "@/features/auth/LoginView.vue";
import { useSessionStore } from "@/stores/session";

import { freshPinia, makeRouter, mountWith } from "./helpers";

it("leaves login for the requested page after mandatory credential change", async () => {
  const pinia = freshPinia();
  const router = makeRouter([
    { path: "/login", component: LoginView },
    { path: "/knowledge", component: { template: "<div>Knowledge</div>" } },
  ]);
  await router.push("/login?next=/knowledge");
  const session = useSessionStore();
  session.mustChangePassword = true;
  session.user = { id: "admin", username: "admin" } as NonNullable<typeof session.user>;
  const wrapper = mountWith(LoginView, {
    pinia,
    router,
    global: { stubs: { ForcedCredentialDialog: true } },
  });
  session.mustChangePassword = false;
  wrapper.findComponent(ForcedCredentialDialog).vm.$emit("completed");
  await flushPromises();
  expect(router.currentRoute.value.path).toBe("/knowledge");
  wrapper.unmount();
});
