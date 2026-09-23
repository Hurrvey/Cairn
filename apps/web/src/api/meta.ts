import { api } from "@/api/client";

export interface MetaResponse {
  product: string;
  version: string;
  api_version: string;
  role: string;
  capabilities: Record<string, boolean>;
}

let cached: Promise<MetaResponse> | null = null;

/** Public capability discovery; cached for the session because it only
 *  changes with a deployment. */
export function meta(): Promise<MetaResponse> {
  cached ??= api.get<MetaResponse>("/v1/meta").catch((error) => {
    cached = null;
    throw error;
  });
  return cached;
}

export function resetMetaCache(): void {
  cached = null;
}
