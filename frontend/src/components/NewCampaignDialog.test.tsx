import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CampaignDetail } from "../types/api";
import { NewCampaignDialog } from "./NewCampaignDialog";


const createdCampaign = {
  job: {
    id: "cmp_test",
    mode: "research_preview",
    status: "queued",
    phase: "queued",
    query: "test",
  },
} as CampaignDetail;


describe("NewCampaignDialog", () => {
  it("submits online source and PDF download options in preview mode", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn(async () => createdCampaign);
    render(
      <NewCampaignDialog
        open
        onClose={vi.fn()}
        onCreate={onCreate}
      />,
    );

    await user.click(screen.getByText("高级选项"));
    const onlineLiterature = screen.getByRole("checkbox", {
      name: "Online literature",
    });
    expect(screen.getByRole("checkbox", { name: "Web search" })).toBeVisible();
    expect(
      screen.queryByRole("checkbox", { name: "Download PDFs" }),
    ).not.toBeInTheDocument();

    await user.click(onlineLiterature);
    const downloadPdfs = screen.getByRole("checkbox", { name: "Download PDFs" });
    await user.click(downloadPdfs);
    await user.click(screen.getByRole("checkbox", { name: "Web search" }));
    await user.type(screen.getByLabelText(/研究目标/), "Search catalyst papers");
    await user.click(screen.getByRole("button", { name: "创建并运行" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        mode: "research_preview",
        online_literature: true,
        web_search: true,
        download_pdfs: true,
      }),
    );
    expect(
      screen.getByRole("checkbox", { name: "Online literature" }),
    ).not.toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: "Web search" }),
    ).not.toBeChecked();
    expect(
      screen.queryByRole("checkbox", { name: "Download PDFs" }),
    ).not.toBeInTheDocument();
  });

  it("clears and hides PDF download when online literature is disabled", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn(async () => createdCampaign);
    render(
      <NewCampaignDialog
        open
        onClose={vi.fn()}
        onCreate={onCreate}
      />,
    );

    await user.click(screen.getByText("高级选项"));
    const onlineLiterature = screen.getByRole("checkbox", {
      name: "Online literature",
    });
    await user.click(onlineLiterature);
    await user.click(screen.getByRole("checkbox", { name: "Download PDFs" }));
    await user.click(onlineLiterature);
    expect(
      screen.queryByRole("checkbox", { name: "Download PDFs" }),
    ).not.toBeInTheDocument();

    await user.type(screen.getByLabelText(/研究目标/), "Offline catalyst plan");
    await user.click(screen.getByRole("button", { name: "创建并运行" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        online_literature: false,
        download_pdfs: false,
      }),
    );
  });
});
