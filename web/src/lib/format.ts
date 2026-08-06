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

/** `4:31`, `1:02:17` — elapsed time for something still going.
 *
 *  `since()` is wrong for this. A scoring batch sits on the same tally for
 *  minutes at a stretch, so with a label reading "5m" for sixty seconds the
 *  only moving part on screen is a spinner, and the honest report from that is
 *  "it looks stuck". Seconds are the cheapest possible proof of life.
 */
export function elapsed(seconds: number | null | undefined): string {
  const total = Math.floor(Math.max(seconds ?? 0, 0));
  const s = String(total % 60).padStart(2, "0");
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}
