export const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`;

export const dateOnly = (ts: string | null | undefined) => (ts ? ts.slice(0, 10) : "—");

export const money = (n: number | null | undefined) =>
  n === null || n === undefined ? null : `$${Math.round(n / 1000)}k`;

export function comp(min: number | null, max: number | null): string | null {
  const lo = money(min);
  const hi = money(max);
  if (lo && hi) return `${lo}–${hi}`;
  if (lo) return `from ${lo}`;
  if (hi) return `up to ${hi}`;
  return null;
}

/** Humanize a state key for display. The vocabulary itself comes from /api/meta;
 *  this only decides how it is spelled on screen. */
export const label = (state: string) => state.replaceAll("_", " ");

/** "45s", "3m", "6h" — from an age the server measured.
 *
 *  Same thresholds as `runs.short_duration`, deliberately: the two describe the
 *  same run on the same page, and disagreeing by a unit reads as a bug. Ages
 *  come from the server rather than being derived from a timestamp here, so a
 *  laptop with a skewed clock cannot report a sweep that finished in the future.
 */
export function since(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "unknown";
  if (seconds < 90) return `${Math.floor(Math.max(seconds, 0))}s`;
  if (seconds < 90 * 60) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 48 * 3600) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}
