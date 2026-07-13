import { expect, test } from "@playwright/test";

const campaignItem = {
  id: "cmp_20260711_frontend",
  campaign_id: "cmp_20260711_frontend",
  query: "合成 K-PBA 粉末并通过离线 XRD 确认目标相",
  status: "awaiting_observation",
  phase: "awaiting_observation",
  mode: "full_campaign",
  execution_adapter: "manual",
  current_iteration: 1,
  max_iterations: 6,
  created_at: "2026-07-11T09:00:00",
  updated_at: "2026-07-11T09:04:00",
};

const detail = {
  job: campaignItem,
  research: {
    status: "completed",
    current_stage: "合成 K-PBA 并完成 XRD 观察",
    stage_route: ["合成 K-PBA 并完成 XRD 观察", "电化学性能观察"],
    current_stage_plan: "配制前驱体，完成共沉淀、洗涤、干燥并回传离线 XRD。",
    macro_plan: [
      { "步骤序号": 1, "操作": "配制前驱体", "试剂/对象": "K4Fe(CN)6", "参数": "0.5 mmol in 10 mL", "来源": "protocol: sample" },
    ],
    observations: [],
    plan_revisions: [],
    logs: ["[2026-07-11 09:00:00] Entered B1 bootstrap"],
    errors: [],
  },
  device: {
    status: "success",
    macro_plan_summary: "映射为液体进样与磁力搅拌 workflow",
    workflow_json: {
      steps: [{ step_number: 1, workstation: "液体进样站", operation: "加液", parameters: { volume_ml: 5 }, source_macro_step: 1 }],
      offline_handoffs: [{ name: "离线 XRD", sample: "干燥粉末" }],
    },
    workflow_txt: "1. 液体进样站：加液 5 mL",
    device_self_check: { container_continuity: "pass" },
    reagent_slot_plan: [],
    container_plan: [],
  },
  campaign: { summary: null, plan_revisions: [], iterations: [{ iteration: 1, phase: "awaiting_observation" }], final_report: null },
  logs: { lines: ["[campaign] waiting for observation"], cursor: 1 },
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/health", (route) => route.fulfill({ json: { status: "ok", version: "0.1" } }));
  await page.route("**/api/v1/assets/workstation-map", (route) => route.fulfill({
    contentType: "image/png",
    body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64"),
  }));
  await page.route("**/api/v1/campaigns/*/events", (route) => route.fulfill({
    contentType: "text/event-stream",
    body: `event: campaign\ndata: ${JSON.stringify(detail)}\n\n`,
  }));
  await page.route("**/api/v1/campaigns/cmp_20260711_frontend", (route) => route.fulfill({ json: detail }));
  await page.route("**/api/v1/campaigns", (route) => route.fulfill({ json: { items: [campaignItem], total: 1 } }));
});

test("renders the live campaign console and all core work areas", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("CHEM/AGENT")).toBeVisible();
  await expect(page.getByRole("heading", { name: campaignItem.query })).toBeVisible();
  await expect(page.getByRole("heading", { name: "回传实验结果" })).toBeVisible();

  await page.getByRole("button", { name: "计划" }).click();
  await expect(page.getByText("配制前驱体").first()).toBeVisible();

  await page.getByRole("button", { name: "设备", exact: true }).click();
  await expect(page.getByRole("heading", { name: "工作站资源表" })).toBeVisible();
  await expect(page.getByText("液体进样站").first()).toBeVisible();

  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

test("opens a safe research preview form without horizontal overflow", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "创建任务" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("radio", { name: /Research Preview/ })).toHaveAttribute("aria-checked", "true");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
});
