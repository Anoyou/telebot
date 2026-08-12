export interface CsvColumn<T> {
  header: string;
  value: (row: T) => unknown;
}

function stringifyCsvValue(value: unknown): string {
  if (value == null) return "";
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function escapeCsvCell(value: unknown): string {
  let text = stringifyCsvValue(value);
  // 表格软件可能忽略前导空白再解释公式，覆盖常见 ASCII 与 Unicode 空白。
  if (/^[\s\u00a0\u2000-\u200b\u2028\u2029\u202f\u205f\u3000]*[=+\-@]/u.test(text)) {
    text = `'${text}`;
  }
  return `"${text.replaceAll('"', '""')}"`;
}

export function serializeCsv<T>(rows: T[], columns: CsvColumn<T>[]): string {
  const header = columns.map((column) => escapeCsvCell(column.header)).join(",");
  const body = rows.map((row) => (
    columns.map((column) => escapeCsvCell(column.value(row))).join(",")
  ));
  return `\uFEFF${[header, ...body].join("\r\n")}\r\n`;
}

export function downloadCsv<T>(
  filename: string,
  rows: T[],
  columns: CsvColumn<T>[],
): void {
  const blob = new Blob([serializeCsv(rows, columns)], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.style.display = "none";
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
