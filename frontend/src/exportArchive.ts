import JSZip from "jszip";
import { download, fetchFile } from "./api";
import type { FileRef } from "./api";
import { zipTimestamp, TIMESTAMP_TIME_ZONE } from "./timestamps";

type ArchiveResult = { file: FileRef; label: string; zipPath?: string; lastWrittenAt?: string; detail?: string; warnings?: string[]; metadata?: unknown };
export const MAX_ARCHIVE_BYTES = 250 * 1024 * 1024;

function uniquePath(raw: string, used: Set<string>) {
  const safe = raw.replace(/\\/g, "/").split("/")
    .filter(p => p && p !== "." && p !== "..").map(p => p.replace(/[<>:"|?*\x00-\x1f]/g, "_")).join("/") || "result";
  let path = safe, suffix = 2;
  while (used.has(path.toLocaleLowerCase())) {
    const dot = safe.lastIndexOf("."), slash = safe.lastIndexOf("/");
    path = dot > slash ? `${safe.slice(0, dot)} (${suffix++})${safe.slice(dot)}` : `${safe} (${suffix++})`;
  }
  used.add(path.toLocaleLowerCase());
  return path;
}
export async function downloadResultArchive(items: ArchiveResult[], filename: string) {
  if (!items.length) throw new Error("Пакет порожній.");
  if (items.reduce((total, item) => total + item.file.size, 0) > MAX_ARCHIVE_BYTES)
    throw new Error("Пакет перевищує 250 МБ. Завантажуйте файли окремо.");
  const archive = new JSZip(), used = new Set<string>(["generation-report.json"]);
  const archiveDate = zipTimestamp(new Date().toISOString());
  const report = [];
  for (const item of items) {
    const path = uniquePath(item.zipPath ?? item.file.name, used);
    // Dates belong to ZIP entries; normal browser downloads cannot set OS mtime.
    archive.file(path, await fetchFile(item.file), {
      date: item.lastWrittenAt ? zipTimestamp(item.lastWrittenAt) : archiveDate, createFolders: false,
    });
    report.push({ name: path, label: item.label, last_written_at: item.lastWrittenAt ?? null,
      timestamp_timezone: TIMESTAMP_TIME_ZONE, detail: item.detail, warnings: item.warnings, data: item.metadata });
  }
  archive.file("generation-report.json", JSON.stringify(report, null, 2), { date: archiveDate });
  download(await archive.generateAsync({ type: "blob" }), filename);
}
