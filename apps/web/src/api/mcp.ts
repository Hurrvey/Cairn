import { api, request } from "@/api/client";

export type MCPServiceState =
  | "stopped"
  | "starting"
  | "running"
  | "stopping"
  | "error"
  | "unknown";
export type MCPDesiredState = "running" | "stopped";
export type MCPServiceAction = "start" | "stop" | "restart";
export type MCPLogLevel = "INFO" | "WARNING" | "ERROR";

export interface MCPConfig {
  port: number;
  auto_start: boolean;
  allowed_hosts: string[];
  allowed_origins: string[];
  request_timeout_s: number;
  max_request_bytes: number;
}

export interface MCPServiceSnapshot {
  config: MCPConfig;
  generation: number;
  observed_generation: number | null;
  desired_state: MCPDesiredState;
  state: MCPServiceState;
  effective_port: number | null;
  started_at: string | null;
  heartbeat_at: string | null;
  last_error: string | null;
  allowed_ports: number[];
  managed: boolean;
}

export interface MCPLogItem {
  id: number;
  at: string;
  level: MCPLogLevel;
  event: string;
  detail: string;
  request_id: string | null;
  status: number | null;
  duration_ms: number | null;
}

export interface MCPLogFilters {
  after_id?: number;
  limit?: number;
  level?: MCPLogLevel;
  since?: string;
}

export const mcpService = {
  get: (signal?: AbortSignal) =>
    api.get<MCPServiceSnapshot>("/v1/mcp-service", { signal }),
  updateConfig: (expectedGeneration: number, config: MCPConfig) =>
    request<MCPServiceSnapshot>("/v1/mcp-service/config", {
      method: "PUT",
      body: { expected_generation: expectedGeneration, config },
    }),
  act: (expectedGeneration: number, action: MCPServiceAction) =>
    api.post<MCPServiceSnapshot>("/v1/mcp-service/actions", {
      expected_generation: expectedGeneration,
      action,
    }),
  logs: (filters: MCPLogFilters = {}, signal?: AbortSignal) =>
    api.get<{ items: MCPLogItem[] }>(
      `/v1/mcp-service/logs${query(filters)}`,
      { signal },
    ),
  logsDownloadUrl: (filters: MCPLogFilters = {}) =>
    `/v1/mcp-service/logs/download${query(filters)}`,
};

function query(params: MCPLogFilters): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
}
