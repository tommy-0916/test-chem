import { describe, expect, it } from "vitest";
import {
  campaignStatus,
  canCancel,
  devicePackage,
  isAwaitingObservation,
  researchState,
  statusLabel,
  statusTone,
} from "./campaign";

describe("campaign view normalization", () => {
  it("keeps agent status separate from campaign stop labels", () => {
    expect(campaignStatus({ status: "completed", stop_reason: "goal_reached" })).toBe("goal_reached");
    expect(statusLabel("feasibility_deadlock")).toBe("可行性死锁");
    expect(statusTone("manual_required")).toBe("warning");
    expect(canCancel({ status: "device_mapping" })).toBe(true);
    expect(canCancel({ status: "goal_reached" })).toBe(false);
  });

  it("unwraps legacy research and device state shapes", () => {
    const detail = {
      job: { id: "cmp_test", status: "running" },
      research: { state: { current_stage: "XRD" } },
      device: { terminal_package: { status: "success", workflow_json: { steps: [] } } },
      campaign: null,
      logs: { lines: [], cursor: 0 },
    };
    expect(researchState(detail)?.current_stage).toBe("XRD");
    expect(devicePackage(detail)?.status).toBe("success");
    expect(devicePackage({ ...detail, device: { workflow_json: { steps: [] }, workflow_txt: "" } })).toBeNull();
  });

  it("detects an observation handoff from phase or status", () => {
    expect(isAwaitingObservation({ job: { id: "a", status: "running", phase: "awaiting_observation" } })).toBe(true);
    expect(isAwaitingObservation({ job: { id: "b", status: "completed" } })).toBe(false);
  });
});
