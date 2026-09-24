import type {
  CampaignDetail,
  CampaignListResponse,
  CreateCampaignInput,
  HealthResponse,
  ObservationInput,
  UploadResult,
} from "../types/api";

const configuredBase = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const API_ROOT = `${configuredBase}/api/v1`;

export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");

  const response = await fetch(`${API_ROOT}${path}`, { ...init, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const detail =
      typeof payload === "object" && payload !== null && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : response.statusText || "请求失败";
    throw new ApiError(detail, response.status, payload);
  }
  return payload as T;
}

export const api = {
  health: () => request<HealthResponse>("/health"),
  campaigns: () => request<CampaignListResponse>("/campaigns"),
  campaign: (id: string) => request<CampaignDetail>(`/campaigns/${encodeURIComponent(id)}`),
  createCampaign: (input: CreateCampaignInput) =>
    request<CampaignDetail>("/campaigns", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  cancelCampaign: (id: string) =>
    request<CampaignDetail>(`/campaigns/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason: "user_requested" }),
    }),
  submitObservation: (id: string, observation: ObservationInput) =>
    request<CampaignDetail>(`/campaigns/${encodeURIComponent(id)}/observations`, {
      method: "POST",
      body: JSON.stringify({ observation }),
    }),
  upload: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<UploadResult>("/uploads", { method: "POST", body: form });
  },
};

export function campaignEventsUrl(id: string): string {
  return `${API_ROOT}/campaigns/${encodeURIComponent(id)}/events`;
}

export function workstationMapUrl(): string {
  return `${API_ROOT}/assets/workstation-map`;
}
