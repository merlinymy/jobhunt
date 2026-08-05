/** Colour per state. Low-saturation fills mixed from the semantic tokens, so a
 *  pill reads correctly against either background and no hex appears here. */
export function stateTone(state: string): string {
  if (state === "offer") return "text-good";
  if (state === "interview") return "text-good";
  if (state === "applied" || state === "packet_ready") return "text-accent";
  if (state === "rejected" || state === "expired") return "text-bad";
  if (state === "skipped" || state === "filtered") return "text-dim";
  return "text-warn";
}

export function scoreTone(score: number): string {
  if (score >= 80) return "text-good";
  if (score >= 50) return "text-warn";
  return "text-dim";
}
