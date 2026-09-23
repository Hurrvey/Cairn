/** Model providers, models and storage bindings (administrator setup). */
import { api } from "@/api/client";
import type { components } from "@/api/schema";

type S = components["schemas"];

export type Provider = S["ProviderResponse"];
export type Model = S["ModelResponse"];
export type CreateProviderRequest = S["CreateProviderRequest"];
export type CreateModelRequest = S["CreateModelRequest"];
export type ModelTestResponse = S["ModelTestResponse"];
export type Binding = S["BindingResponse"];
export type CreateBindingRequest = S["CreateBindingRequest"];

export const providers = {
  list: (signal?: AbortSignal) => api.get<Provider[]>("/v1/model-providers", { signal }),
  create: (body: CreateProviderRequest) => api.post<Provider>("/v1/model-providers", body),
  remove: (id: string) => api.delete<void>(`/v1/model-providers/${encodeURIComponent(id)}`),
};

export const models = {
  list: (signal?: AbortSignal) => api.get<Model[]>("/v1/models", { signal }),
  create: (body: CreateModelRequest) => api.post<Model>("/v1/models", body),
  remove: (id: string) => api.delete<void>(`/v1/models/${encodeURIComponent(id)}`),
  test: (id: string) => api.post<ModelTestResponse>(`/v1/models/${encodeURIComponent(id)}/test`),
};

export const bindings = {
  list: (signal?: AbortSignal) => api.get<Binding[]>("/v1/storage-bindings", { signal }),
  create: (body: CreateBindingRequest) => api.post<Binding>("/v1/storage-bindings", body),
  remove: (id: string) => api.delete<void>(`/v1/storage-bindings/${encodeURIComponent(id)}`),
  test: (id: string) => api.post<{ healthy: boolean }>(`/v1/storage-bindings/${encodeURIComponent(id)}/test`),
};
