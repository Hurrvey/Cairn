/**
 * The document pipeline as the interface shows it.
 *
 * Six stones, one per stage the server reports. The mapping is the only place
 * that knows the state names, so a renamed state shows up here rather than in
 * a dozen templates.
 */
import type { Tone } from "@/components/ui/types";

export const PIPELINE_STAGES = ["fetch", "parse", "chunk", "embed", "index", "done"] as const;
export type PipelineStage = (typeof PIPELINE_STAGES)[number];

const STAGE_INDEX: Record<string, number> = {
  registered: 0,
  fetching: 0,
  fetched: 1,
  parsing: 1,
  parsed: 2,
  chunking: 2,
  chunked: 3,
  embedding: 3,
  embedded: 4,
  indexing: 4,
  indexed: 6,
  skipped: 6,
};

export interface StageInfo {
  /** Number of completed stones, 0..6. */
  filled: number;
  active: boolean;
  failed: boolean;
  deleting: boolean;
}

export function stageOf(state: string): StageInfo {
  if (state === "failed") return { filled: 0, active: false, failed: true, deleting: false };
  if (state === "deleting") return { filled: 0, active: false, failed: false, deleting: true };
  const index = STAGE_INDEX[state] ?? 0;
  const active = state.endsWith("ing");
  return { filled: index, active, failed: false, deleting: false };
}

export function documentTone(state: string): Tone {
  if (state === "indexed") return "ok";
  if (state === "failed") return "bad";
  if (state === "skipped") return "neutral";
  if (state === "deleting") return "warn";
  return "info";
}

export function isTerminal(state: string): boolean {
  return state === "indexed" || state === "failed" || state === "skipped";
}

export function kbTone(status: string): Tone {
  switch (status) {
    case "active":
      return "ok";
    case "indexing":
      return "info";
    case "error":
      return "bad";
    case "deleting":
    case "archived":
      return "warn";
    default:
      return "neutral";
  }
}

export function mcpTone(state: string): Tone {
  if (state === "running") return "ok";
  if (state === "starting" || state === "stopping") return "warn";
  if (state === "error") return "bad";
  return "neutral";
}

export function healthTone(state: string | undefined): Tone {
  if (state === "healthy" || state === "ok") return "ok";
  if (state === "unavailable" || state === "error" || state === "unhealthy") return "bad";
  if (state === "degraded") return "warn";
  return "neutral";
}
