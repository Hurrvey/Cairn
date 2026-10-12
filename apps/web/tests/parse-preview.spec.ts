import { afterEach, expect, it, vi } from "vitest";
import ParsePreview from "@/features/knowledge/ParsePreview.vue";
import { mountWith, jsonResponse, settle } from "./helpers";

afterEach(() => vi.unstubAllGlobals());

it("renders source structure safely and transforms rotated crop citations", async () => {
  vi.stubGlobal("fetch", vi.fn(async (path: string) => {
    if (!path.includes("elements")) return jsonResponse({ parse_version: "a".repeat(64), revision: 2,
      pages: 1, engine: "docling", warnings: [], quality_status: "unscored" });
    return jsonResponse({ items: [{ element_id: "b".repeat(64), type: "formula", text: "",
      formula: { native: "<script>unsafe()</script>", recognized: null, status: "unrecognized" },
      table: null, locations: [{ page: 1, width: 100, height: 200, crop: [10, 20, 110, 220],
        rotation: 90, bbox: [30, 60, 70, 120] }] }], next_cursor: null });
  }));
  const wrapper = mountWith(ParsePreview, { props: { documentId: "doc_test" } });
  await settle();
  expect(wrapper.find("script").exists()).toBe(false);
  expect(wrapper.text()).toContain("<script>unsafe()</script>");
  const overlay = wrapper.find(".pointer-events-none");
  expect(overlay.attributes("style")).toContain("left: 20%");
  expect(overlay.attributes("style")).toContain("top: 20%");
  expect(wrapper.find("img").attributes("src")).toContain("view=active&parse_version=");
});
