export type TimestampConfig = { enabled: boolean; start: string; step: string };
export const TIMESTAMP_TIME_ZONE = "Europe/Kyiv";

function parts(moment: Date) {
  return Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: TIMESTAMP_TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).formatToParts(moment).filter(p => p.type !== "literal").map(p => [p.type, Number(p.value)]));
}
function wallClock(moment: Date) {
  const p = parts(moment);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
}
export function localTimestamp(moment = new Date()) {
  const p = parts(moment), pad = (n: number) => String(n).padStart(2, "0");
  return `${p.year}-${pad(p.month)}-${pad(p.day)}T${pad(p.hour)}:${pad(p.minute)}`;
}
export function parseTimestampStart(value: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
  if (!match) throw new Error("LastWriteTime: вкажіть дату й час початку.");
  const [year, month, day, hour, minute, second] = match.slice(1).map(v => Number(v ?? 0));
  if (year < 1980 || year > 2107) throw new Error("LastWriteTime: ZIP підтримує роки 1980–2107.");
  const requested = Date.UTC(year, month - 1, day, hour, minute, second);
  let moment = requested;
  // Convert Kyiv wall-clock input to an actual instant, including DST.
  for (let i = 0; i < 3; i++) moment = requested - (wallClock(new Date(moment)) - moment);
  const p = parts(new Date(moment));
  if (p.year !== year || p.month !== month || p.day !== day || p.hour !== hour || p.minute !== minute || p.second !== second)
    throw new Error("LastWriteTime: некоректна дата або час під час переходу на літній час.");
  return moment;
}
export function parseTimestampStep(value: string): [number, number] {
  const match = /^(\d+(?:[.,]\d+)?)\s*(?:[-–]\s*(\d+(?:[.,]\d+)?))?$/.exec(value.trim());
  if (!match) throw new Error("LastWriteTime: крок у хвилинах — число або діапазон, наприклад 6-8.");
  const low = Number(match[1].replace(",", ".")), high = Number((match[2] ?? match[1]).replace(",", "."));
  if (!Number.isFinite(low) || !Number.isFinite(high)) throw new Error("LastWriteTime: завеликий крок.");
  return [Math.min(low, high), Math.max(low, high)];
}
export function planLastWrittenAt(config: TimestampConfig, count: number, random = Math.random): (string | undefined)[] {
  if (!config.enabled) return Array.from({ length: count }, () => undefined);
  let moment = parseTimestampStart(config.start);
  const [low, high] = parseTimestampStep(config.step);
  return Array.from({ length: count }, (_, index) => {
    if (index) moment += Math.round((low + random() * (high - low)) * 60_000);
    const date = new Date(moment);
    if (!Number.isFinite(moment) || date.getUTCFullYear() < 1980 || date.getUTCFullYear() > 2107 || parts(date).year > 2107)
      throw new Error("LastWriteTime: дата пакета виходить за межі ZIP (1980–2107).");
    return date.toISOString();
  });
}
export function lastWrittenAt(config: TimestampConfig, index: number) {
  return planLastWrittenAt(config, index + 1)[index];
}
// JSZip writes UTC fields into ZIP's timezone-less DOS date. Preserve the
// selected Kyiv wall-clock value rather than silently shifting it by 2–3 hours.
export function zipTimestamp(value: string) {
  const instant = new Date(value);
  if (!Number.isFinite(instant.getTime())) throw new Error("Некоректна дата файлу.");
  return new Date(wallClock(instant));
}
