import fs from "node:fs/promises";
import path from "node:path";

import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const [outputPath, rowsPath, previewPath = ""] = process.argv.slice(2);
if (!outputPath || !rowsPath) {
  throw new Error(
    "usage: build_solid_recipe_artifact.mjs OUTPUT.xlsx ROWS.json [PREVIEW.png]",
  );
}

const rawRows = JSON.parse(await fs.readFile(rowsPath, "utf8"));
if (!Array.isArray(rawRows) || rawRows.length === 0) {
  throw new Error("ROWS.json must contain at least one recipe row");
}

const rows = rawRows.map((row, index) => {
  const bottleNumber = Number(row.bottle_number ?? index + 1);
  const massG = Number(row.mass_g);
  const hopperNumber = Number(row.hopper_number);
  if (!Number.isInteger(bottleNumber) || bottleNumber !== index + 1) {
    throw new Error("bottle_number must be consecutive and start at 1");
  }
  if (!Number.isFinite(massG) || massG < 0 || massG > 50) {
    throw new Error("mass_g must be within [0, 50]");
  }
  if (!Number.isInteger(hopperNumber) || hopperNumber < 1 || hopperNumber > 30) {
    throw new Error("hopper_number must be an integer within [1, 30]");
  }
  return [bottleNumber, massG, hopperNumber];
});

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("固体进样");
sheet.showGridLines = false;
sheet.getRange("A1:C1").values = [["瓶号", "加样量(g)", "料罐号"]];
sheet.getRange(`A2:C${rows.length + 1}`).values = rows;
sheet.getRange("A1:C1").format = {
  fill: "#1F4E78",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { preset: "outside", style: "thin", color: "#9EADBA" },
};
const dataRange = sheet.getRange(`A2:C${rows.length + 1}`);
dataRange.format = {
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { preset: "inside", style: "thin", color: "#D9E2F3" },
};
sheet.getRange(`A2:A${rows.length + 1}`).format.numberFormat = "0";
sheet.getRange(`B2:B${rows.length + 1}`).format.numberFormat = "0.000000";
sheet.getRange(`C2:C${rows.length + 1}`).format.numberFormat = "0";
sheet.getRange(`A1:A${rows.length + 1}`).format.columnWidth = 15;
sheet.getRange(`B1:B${rows.length + 1}`).format.columnWidth = 15;
sheet.getRange(`C1:C${rows.length + 1}`).format.columnWidth = 15;
sheet.getRange("A1:C1").format.rowHeight = 24;
sheet.freezePanes.freezeRows(1);

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);

if (previewPath) {
  await fs.mkdir(path.dirname(previewPath), { recursive: true });
  const preview = await workbook.render({
    sheetName: "固体进样",
    range: `A1:C${rows.length + 1}`,
    scale: 2,
    format: "png",
  });
  await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
}

process.stdout.write(
  JSON.stringify({ output_path: outputPath, row_count: rows.length }) + "\n",
);
