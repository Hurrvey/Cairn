/**
 * Where a knowledge base's keyword (sparse) vectors come from.
 *
 * `auto` mirrors the server (CatalogService._resolve_sparse): the embedding
 * model's own sparse output, else an enabled sparse model — the local bge-m3
 * service before paid vendors — else built-in BM25. The server decides; this
 * only lets the form say what "automatic" will pick.
 */
import type { ModelChoice } from "@/api/knowledge";

export type SparseChoice = { kind: "auto" | "bm25" | "model"; model_id?: string | null };

export function autoSparseModel(embeddingModelId: string, models: ModelChoice[]): ModelChoice | null {
  const own = models.find((model) => model.id === embeddingModelId);
  if (own?.sparse) return own;
  const candidates = models
    .filter((model) => model.sparse)
    .sort((a, b) => {
      const local = Number(a.provider_family !== "bge_m3") - Number(b.provider_family !== "bge_m3");
      return local || a.display_name.localeCompare(b.display_name);
    });
  return candidates[0] ?? null;
}

/** Encode a select value ("auto", "bm25" or "model:<id>") as a request body. */
export function parseSparseValue(value: string): SparseChoice {
  if (value.startsWith("model:")) return { kind: "model", model_id: value.slice("model:".length) };
  return { kind: value === "bm25" ? "bm25" : "auto" };
}
