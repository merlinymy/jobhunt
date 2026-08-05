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
