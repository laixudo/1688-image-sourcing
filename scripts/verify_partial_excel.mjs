import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = process.argv[2];
const previewPath = process.argv[3];
if (!inputPath || !previewPath) throw new Error("usage: verify_partial_excel.mjs <xlsx> <preview.png>");

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const sheets = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 3000 });
const table = await workbook.inspect({
  kind: "table",
  range: "图搜结果!A1:S10",
  include: "values,formulas",
  tableMaxRows: 10,
  tableMaxCols: 19,
  maxChars: 6000,
});
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "formula error scan",
  maxChars: 3000,
});
const sheet = workbook.worksheets.getItem("图搜结果");
const used = sheet.getUsedRange();
const preview = await workbook.render({
  sheetName: "图搜结果",
  range: "A1:S10",
  scale: 1,
  format: "png",
});
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
console.log(JSON.stringify({
  sheets: sheets.ndjson,
  firstRows: table.ndjson,
  formulaErrors: errors.ndjson,
  usedRows: used.rowCount,
  usedColumns: used.columnCount,
  previewPath,
}, null, 2));
