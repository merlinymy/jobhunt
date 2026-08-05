/** Query keys in one place, so an invalidation cannot miss by a typo. */

export type Params = Record<string, string>;

export const k = {
  meta: () => ["meta"] as const,
  pipeline: (p: Params) => ["pipeline", p] as const,
  applications: (p: Params) => ["applications", p] as const,
  application: (id: number) => ["application", id] as const,
  review: (limit: number) => ["review", limit] as const,
  packet: (id: number) => ["packet", id] as const,
  stats: (p: Params) => ["stats", p] as const,
  fill: () => ["fill"] as const,
  contacts: () => ["contacts"] as const,
  runs: () => ["runs"] as const,
  urlCheck: (url: string) => ["urlCheck", url] as const,
};
