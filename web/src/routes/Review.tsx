import { Check, ExternalLink, Handshake, SkipForward, Undo2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { toast } from "sonner";

import { useDecide } from "../api/mutations";
import { useReview } from "../api/queries";
import type { ReviewCard } from "../api/types";
import { Discovery } from "../components/Discovery";
import { Sheet } from "../components/Sheet";
import { Button, Card, ErrorState, Pill, Spinner } from "../components/ui";
import { scoreTone } from "../lib/states";

type Decided = ReviewCard & { decided?: "approved" | "skipped"; pending?: boolean };

/** Skip is terminal. `(skipped, *)` is absent from states.TRANSITIONS, so there
 *  is no way back — and a big thumb-reachable Skip button is a new risk that the
 *  old tiny controls accidentally mitigated. Hold the mutation behind an undo
 *  window instead of adding a state transition the server refuses. */
const UNDO_MS = 3000;

export default function Review() {
  const [params, setParams] = useSearchParams();
  const limit = Number(params.get("limit") ?? 8);
  const { data, isPending, error, refetch } = useReview(limit);
  const decide = useDecide(limit);
  const [reading, setReading] = useState<ReviewCard | null>(null);
  const pendingSkip = useRef(new Map<number, number>());

  useEffect(() => {
    const timers = pendingSkip.current;
    return () => timers.forEach((id) => window.clearTimeout(id));
  }, []);

  function approve(card: ReviewCard) {
    decide.mutate({ id: card.application_id, outcome: "approve" });
  }

  function skip(card: ReviewCard) {
    const timer = window.setTimeout(() => {
      pendingSkip.current.delete(card.application_id);
      decide.mutate({ id: card.application_id, outcome: "skip" });
    }, UNDO_MS);
    pendingSkip.current.set(card.application_id, timer);

    toast(`Skipped ${card.title}`, {
      duration: UNDO_MS,
      action: {
        label: "Undo",
        onClick: () => {
          const handle = pendingSkip.current.get(card.application_id);
          if (handle !== undefined) {
            window.clearTimeout(handle);
            pendingSkip.current.delete(card.application_id);
          }
        },
      },
      icon: <Undo2 className="size-4" />,
    });
  }

  if (isPending) return <Spinner label="Loading the queue" />;
  if (error) return <ErrorState error={error} retry={() => void refetch()} />;

  const batch = (data?.batch ?? []) as Decided[];

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">Review</h1>
        <p className="text-sm text-dim">
          {data?.waiting ?? 0} waiting · showing {batch.length}
          {(data?.waiting ?? 0) > limit && (
            <button
              className="ml-2 text-accent hover:underline"
              onClick={() => setParams({ limit: String(limit + 8) })}
            >
              show more
            </button>
          )}
        </p>
      </div>

      {batch.length === 0 ? (
        /* An empty queue is the one moment the button is certainly wanted, so it
           goes here rather than sending you to another page to find it. */
        <div className="card grid justify-items-center gap-4 p-8 text-center">
          <div>
            <p className="font-medium">Queue empty.</p>
            <p className="mt-1 text-sm text-dim">Nothing scored is waiting.</p>
          </div>
          <Discovery className="w-full max-w-md text-left" />
        </div>
      ) : (
        <div className="grid gap-3">
          {batch.map((card) => (
            <Card
              key={card.application_id}
              className={card.decided ? "opacity-60" : undefined}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <a
                    href={card.apply_url}
                    target="_blank"
                    rel="noopener"
                    className="text-lg font-medium text-accent hover:underline"
                  >
                    {card.title}
                  </a>
                  <p className="mt-0.5 text-sm text-dim">
                    {card.company} · {card.where}
                    {card.comp && ` · ${card.comp}`}
                  </p>
                  {card.also_in > 0 && (
                    <p className="mt-1.5">
                      {/* One shot, not N. Deciding this decides all of them. */}
                      <Pill tone="text-dim">
                        also in {card.also_in} other location{card.also_in === 1 ? "" : "s"}
                      </Pill>
                    </p>
                  )}
                </div>
                <span
                  className={`shrink-0 text-2xl font-semibold tabular ${scoreTone(card.score)}`}
                >
                  {Math.round(card.score)}
                </span>
              </div>

              {card.referral && (
                <p className="mt-3 flex items-center gap-2 rounded-lg bg-good/10 px-3 py-2 text-sm text-good">
                  <Handshake className="size-4 shrink-0" aria-hidden />
                  You know <strong>{card.referral}</strong> here — ping first.
                </p>
              )}

              {card.reason && <p className="mt-3 text-sm">{card.reason}</p>}

              <div className="mt-4 flex flex-wrap items-center gap-2">
                {card.decided ? (
                  <Pill tone={card.decided === "approved" ? "text-good" : "text-dim"}>
                    {card.decided}
                  </Pill>
                ) : (
                  <>
                    <Button variant="primary" onClick={() => approve(card)} className="flex-1 sm:flex-none">
                      <Check className="size-4" aria-hidden /> Approve
                    </Button>
                    <Button onClick={() => skip(card)} className="flex-1 sm:flex-none">
                      <SkipForward className="size-4" aria-hidden /> Skip
                    </Button>
                  </>
                )}
                {card.excerpt && (
                  <Button variant="ghost" size="sm" onClick={() => setReading(card)}>
                    Read description
                  </Button>
                )}
                <a
                  href={card.apply_url}
                  target="_blank"
                  rel="noopener"
                  className="inline-flex items-center gap-1 text-sm text-dim hover:text-accent"
                >
                  posting <ExternalLink className="size-3.5" aria-hidden />
                </a>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* A full-screen dialog rather than <details>, which pushed the rest of the
          queue down the page and lost your place in it. */}
      <Sheet
        open={reading !== null}
        onOpenChange={(open) => !open && setReading(null)}
        title={reading?.title ?? ""}
        side="full"
      >
        <pre className="whitespace-pre-wrap font-sans text-sm leading-relaxed">
          {reading?.excerpt}
        </pre>
      </Sheet>
    </div>
  );
}
