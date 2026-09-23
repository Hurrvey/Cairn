import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge Tailwind classes; later classes win on conflicts. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
}

export function formatNumber(value: number | null | undefined, locale = "en-US"): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat(locale).format(value);
}

export function formatDateTime(
  value: string | null | undefined,
  locale = "en-US",
  style: "short" | "medium" = "medium",
): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(locale, {
    dateStyle: style,
    timeStyle: style === "short" ? "short" : "medium",
  }).format(parsed);
}

const RELATIVE_STEPS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

/** "3 minutes ago" style; falls back to the absolute date beyond a month. */
export function formatRelative(
  value: string | null | undefined,
  locale = "en-US",
  now: number = Date.now(),
): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const seconds = Math.round((parsed.getTime() - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(0, "second");
  if (abs > 45 * 24 * 3600) return formatDateTime(value, locale, "short");
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  for (const [unit, size] of RELATIVE_STEPS) {
    if (abs >= size) return formatter.format(Math.round(seconds / size), unit);
  }
  return formatter.format(seconds, "second");
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  const hours = Math.floor(seconds / 3600);
  return `${hours}h ${Math.floor((seconds % 3600) / 60)}m`;
}

/** Shorten a public ID for display while keeping it recognisable. */
export function shortId(id: string | null | undefined, keep = 6): string {
  if (!id) return "";
  const parts = id.split("_");
  const prefix = parts.length > 1 ? `${parts[0]}_` : "";
  const body = parts.length > 1 ? parts.slice(1).join("_") : id;
  return body.length <= keep * 2 ? id : `${prefix}${body.slice(0, keep)}…${body.slice(-4)}`;
}

export function splitLines(value: string): string[] {
  return value
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}
