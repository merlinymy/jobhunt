import { AlertTriangle, ShieldCheck } from "lucide-react";

import type { Gap } from "../api/types";
import { CopyButton } from "./CopyButton";
import { Card, Pill } from "./ui";

/** What the posting wants that the corpus cannot support, and the nearest thing it can.
 *
 *  The resume already refuses to fabricate the missing thing, which is right and
 *  leaves silence behind: a reader scanning for AWS and finding nothing about
 *  infrastructure concludes there is none. The `bullet_ids` behind each of these
 *  were handed to the tailor before it chose, so the adjacent evidence is on the
 *  page. This panel is the other half — the words to use when a form asks.
 *
 *  `say` is copyable because that is the entire workflow: a box on an ATS form
 *  asking "describe your AWS experience", and ten minutes that should not be
 *  spent rewriting an honest answer you already wrote once.
 */
export function Gaps({ gaps, analysed }: { gaps: Gap[]; analysed: boolean }) {
  if (!analysed) return null;

  if (gaps.length === 0) {
    return (
      <Card className="mb-4 border-good/30">
        <p className="flex items-center gap-2 text-sm text-good">
          <ShieldCheck className="size-4 shrink-0" aria-hidden />
          Nothing this posting asks for is missing from your corpus.
        </p>
      </Card>
    );
  }

  const required = gaps.filter((g) => g.severity === "required").length;
  return (
    <Card className="mb-4">
      <p className="font-medium">
        {gaps.length} thing{gaps.length === 1 ? "" : "s"} this posting wants that you
        cannot claim
      </p>
      <p className="mt-1 text-sm text-dim">
        {required > 0 && `${required} of them stated as requirements. `}
        The resume shows the nearest evidence instead of going silent. These are the
        words for when a form asks directly.
      </p>

      <ul className="mt-4 space-y-4">
        {gaps.map((g, i) => (
          <li key={i} className="border-t border-line pt-4 first:border-0 first:pt-0">
            <div className="flex flex-wrap items-center gap-2">
              <Pill tone={g.severity === "required" ? "text-warn" : "text-dim"}>
                {g.severity}
              </Pill>
              <span className="font-medium">{g.wanted}</span>
            </div>

            <p className="mt-1 text-sm text-dim">
              {g.bullet_ids.length > 0 ? (
                <>
                  Closest: {g.have}{" "}
                  <span className="text-dim/70">
                    ({g.bullet_ids.length} source line
                    {g.bullet_ids.length === 1 ? "" : "s"})
                  </span>
                </>
              ) : (
                // An honest dead end. Saying so beats a stretch, and the panel
                // should not dress it up as an answer.
                <span className="text-bad">
                  No adjacent experience — this one has no good answer.
                </span>
              )}
            </p>

            {g.say && (
              <div className="mt-2 rounded-lg bg-panel-2 p-3">
                <p className="select-text text-[15px] leading-relaxed">{g.say}</p>
                <div className="mt-2 flex flex-wrap items-center gap-3">
                  <CopyButton value={g.say}>Copy this answer</CopyButton>
                  {g.unsourced.length > 0 && (
                    /* Deterministic check: a figure here goes to a human who can
                       ask a follow-up, so an invented one is worse than on paper. */
                    <span className="flex items-center gap-1.5 text-sm text-bad">
                      <AlertTriangle className="size-4 shrink-0" aria-hidden />
                      {g.unsourced.join(", ")} not found in your corpus — check before
                      using
                    </span>
                  )}
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}
