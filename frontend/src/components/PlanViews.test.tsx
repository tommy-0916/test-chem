import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MacroPlanTable, StageRoute } from "./PlanViews";

describe("plan views", () => {
  it("marks and renders the current stage route", () => {
    render(<StageRoute stages={["合成并 XRD", "Raman 表征"]} currentStage="合成并 XRD" />);
    expect(screen.getByText("合成并 XRD")).toBeInTheDocument();
    expect(screen.getByText("Raman 表征")).toBeInTheDocument();
  });

  it("renders stable macro action fields", () => {
    render(
      <MacroPlanTable
        steps={[{
          "步骤序号": 1,
          "操作": "配制前驱体",
          "试剂/对象": "K4Fe(CN)6",
          "参数": "0.5 mmol in 10 mL",
          "来源": "protocol: sample",
        }]}
      />,
    );
    expect(screen.getByText("配制前驱体")).toBeInTheDocument();
    expect(screen.getByText("K4Fe(CN)6")).toBeInTheDocument();
    expect(screen.getByText("protocol: sample")).toBeInTheDocument();
  });

  it("shows an explicit closure state for an empty plan", () => {
    render(<MacroPlanTable steps={[]} />);
    expect(screen.getByText("当前没有待执行 macro action")).toBeInTheDocument();
  });
});
