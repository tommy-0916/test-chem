import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { OverviewView } from "./OverviewView";

describe("OverviewView", () => {
  it("uses research revisions and does not treat an empty observation as data", () => {
    const { container } = render(
      <OverviewView
        job={{
          id: "cmp_preview",
          mode: "research_preview",
          status: "completed",
          phase: "finished",
          query: "Preview objective",
          current_iteration: 0,
        }}
        research={{
          status: "completed",
          route_message: "Research bootstrap complete",
          stage_route: ["Synthesis to XRD"],
          current_stage: "Synthesis to XRD",
          current_stage_plan: "Prepare the sample.",
          latest_observation: {},
          observations: [],
          plan_revisions: [{ plan_version: 1, event: "initial" }],
        }}
        device={null}
        campaign={{ summary: {}, plan_revisions: [], iterations: [] }}
      />,
    );

    expect(screen.getByText("暂无 observation")).toBeInTheDocument();
    expect(screen.queryByText("已收到结构化 observation")).not.toBeInTheDocument();
    const metrics = container.querySelector(".campaign-readout__metrics");
    expect(metrics).not.toBeNull();
    expect(within(metrics as HTMLElement).getByText("01")).toBeInTheDocument();
  });
});
