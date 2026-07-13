import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, campaignEventsUrl } from "../api/client";
import type {
  CampaignDetail,
  CampaignListItem,
  CreateCampaignInput,
  HealthResponse,
  ObservationInput,
} from "../types/api";
import { itemId } from "../utils/campaign";

type ConnectionMode = "connecting" | "live" | "polling";

export function useCampaignConsole() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [campaigns, setCampaigns] = useState<CampaignListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<CampaignDetail | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [listError, setListError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [connection, setConnection] = useState<ConnectionMode>("polling");
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;

  const loadHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
      setHealthError(false);
    } catch {
      setHealthError(true);
    }
  }, []);

  const loadCampaigns = useCallback(async (silent = false) => {
    if (!silent) setListLoading(true);
    try {
      const response = await api.campaigns();
      const items = Array.isArray(response.items) ? response.items : [];
      setCampaigns(items);
      setTotal(Number(response.total ?? items.length));
      setListError("");
      if (!selectedRef.current && items.length) setSelectedId(itemId(items[0]));
    } catch (error) {
      setListError(error instanceof Error ? error.message : "无法读取 Campaign 列表");
    } finally {
      if (!silent) setListLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (id: string, silent = false) => {
    if (!id) return;
    if (!silent) setDetailLoading(true);
    try {
      const response = await api.campaign(id);
      if (selectedRef.current === id) setDetail(response);
      setDetailError("");
    } catch (error) {
      if (selectedRef.current === id) {
        setDetailError(error instanceof Error ? error.message : "无法读取 Campaign 详情");
      }
    } finally {
      if (!silent && selectedRef.current === id) setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadHealth();
    void loadCampaigns();
    const healthTimer = window.setInterval(loadHealth, 15_000);
    const listTimer = window.setInterval(() => void loadCampaigns(true), 5_000);
    return () => {
      window.clearInterval(healthTimer);
      window.clearInterval(listTimer);
    };
  }, [loadCampaigns, loadHealth]);

  useEffect(() => {
    setDetail(null);
    setDetailError("");
    if (!selectedId) return;

    void loadDetail(selectedId);
    const pollTimer = window.setInterval(() => void loadDetail(selectedId, true), 4_000);
    if (!("EventSource" in window)) {
      setConnection("polling");
      return () => window.clearInterval(pollTimer);
    }

    setConnection("connecting");
    const source = new EventSource(campaignEventsUrl(selectedId));
    source.onopen = () => setConnection("live");
    const handleCampaignEvent = (event: MessageEvent<string>) => {
      try {
        const message: unknown = JSON.parse(event.data);
        const next = isCampaignDetail(message)
          ? message
          : message && typeof message === "object" && "detail" in message
            ? (message as { detail?: unknown }).detail
            : null;
        if (isCampaignDetail(next)) {
          setDetail(next);
          setDetailError("");
          void loadCampaigns(true);
          if (isTerminalDetail(next)) {
            window.clearInterval(pollTimer);
            source.close();
          }
        }
      } catch {
        void loadDetail(selectedId, true);
      }
    };
    source.onmessage = handleCampaignEvent;
    source.addEventListener("campaign", handleCampaignEvent);
    source.onerror = () => setConnection("polling");

    return () => {
      window.clearInterval(pollTimer);
      source.removeEventListener("campaign", handleCampaignEvent);
      source.close();
    };
  }, [loadCampaigns, loadDetail, selectedId]);

  const createCampaign = useCallback(
    async (input: CreateCampaignInput) => {
      const response = await api.createCampaign(input);
      const id = itemId(response.job);
      if (id) setSelectedId(id);
      setDetail(response);
      await loadCampaigns(true);
      return response;
    },
    [loadCampaigns],
  );

  const cancelCampaign = useCallback(
    async (id: string) => {
      const response = await api.cancelCampaign(id);
      if (selectedRef.current === id) setDetail(response);
      await loadCampaigns(true);
      return response;
    },
    [loadCampaigns],
  );

  const submitObservation = useCallback(
    async (id: string, observation: ObservationInput) => {
      const response = await api.submitObservation(id, observation);
      if (selectedRef.current === id) setDetail(response);
      await loadCampaigns(true);
      return response;
    },
    [loadCampaigns],
  );

  const selectedItem = useMemo(
    () => campaigns.find((item) => itemId(item) === selectedId) || detail?.job || null,
    [campaigns, detail, selectedId],
  );

  return {
    health,
    healthError,
    campaigns,
    total,
    selectedId,
    setSelectedId,
    selectedItem,
    detail,
    listLoading,
    detailLoading,
    listError,
    detailError,
    connection,
    refresh: () => Promise.all([loadCampaigns(), selectedId ? loadDetail(selectedId) : Promise.resolve()]),
    createCampaign,
    cancelCampaign,
    submitObservation,
  };
}

function isCampaignDetail(value: unknown): value is CampaignDetail {
  return Boolean(value && typeof value === "object" && "job" in value);
}

function isTerminalDetail(detail: CampaignDetail): boolean {
  if (detail.job.stop_reason) return true;
  return [
    "completed",
    "failed",
    "cancelled",
    "goal_reached",
    "manual_required",
    "feasibility_deadlock",
    "max_iterations",
    "research_error",
    "device_error",
  ].includes(String(detail.job.status || "").toLowerCase());
}
