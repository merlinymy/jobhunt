import clsx from "clsx";
import {
  AlertTriangle, ChevronDown, Loader2, RefreshCw, Search, Sparkles,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { useStartRun } from "../api/mutations";
import { useRuns } from "../api/queries";
import type { Run, RunPipeline, Runs } from "../api/types";
import { since } from "../lib/format";
import { Button } from "./ui";
import { Sheet } from "./Sheet";

/* Finding jobs, from the dashboard.
 *
 * The work happens in a background thread on the server and the lock lives in
 * the database, so this component owns none of it — it starts a run, watches
 * `/api/runs`, and refuses to let you start a second one. Everything it renders
 * comes from the server, down to the phase wording, so adding a phase to a
 * worker does not need an edit here to stop it printing a bare identifier.
 */

const TASK_NOUN: Record<Run["task"], string> = { ingest: "Sweep", score: "Scoring" };

/** A run started elsewhere is worth naming. Finding the dashboard busy at 06:31
 *  is confusing right up until it says the scheduled agent is the one running. */
const TRIGGER_NOTE: Record<Run["trigger"], string> = {
  dashboard: "",
  cli: "started from a terminal",
  launchd: "the scheduled agent",
};

function counts(run: Run | null | undefined): string {
  const entries = Object.entries(run?.progress?.counts ?? {});
  return entries.map(([label, n]) => `${n} ${label}`).join(" · ");
}

/** The live panel: which step, what it is on, how far through, what it has found. */
function Progress({ run, labels }: { run: Run; labels: Record<string, string> }) {
  const progress = run.progress;
  const phase = (progress && labels[progress.phase]) || "Working";
  const total = progress?.total ?? 0;
  const done = progress?.done ?? 0;
  // A bar needs a real denominator. The batch wait has one because the API
  // reports its own per-request tallies; "storing" briefly does not.
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : null;
  const tally = counts(run);

  return (
    <div className="card p-3 sm:p-4" role="status" aria-live="polite">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Loader2 className="size-4 shrink-0 animate-spin text-accent" aria-hidden />
        <span className="font-medium">{phase}</span>
        {run.steps > 1 && (
          <span className="text-sm text-dim">
            step {run.step} of {run.steps}
          </span>
        )}
        {TRIGGER_NOTE[run.trigger] && (
          <span className="text-sm text-dim">· {TRIGGER_NOTE[run.trigger]}</span>
        )}
        <span className="ml-auto text-sm tabular text-dim">{since(run.age_seconds)}</span>
      </div>

      {progress?.message && (
        <p className="mt-1.5 truncate text-sm text-dim" title={progress.message}>
          {progress.message}
        </p>
      )}

      <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-panel-2">
        {pct === null ? (
          /* No denominator: a sliding bar says "still going" without claiming
             to know how far through it is. */
          <div className="h-full w-1/3 animate-pulse rounded-full bg-accent/60" />
        ) : (
          <div
            className="h-full rounded-full bg-accent transition-[width] duration-500"
            style={{ width: `${pct}%` }}
          />
        )}
      </div>

      <div className="mt-2 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <span className="text-sm tabular text-dim">{tally || "counting…"}</span>
        {pct !== null && (
          <span className="text-sm tabular text-dim">
            {done} / {total}
          </span>
        )}
      </div>
    </div>
  );
}

/** The idle line: did the last one work, and when. */
function LastRun({ data }: { data: Runs }) {
  const recent = [data.last.ingest, data.last.score]
    .filter((run): run is Run => run !== null)
    .sort((a, b) => (a.age_seconds ?? 0) - (b.age_seconds ?? 0));
  const broken = recent.find((run) => run.state === "failed" || run.state === "interrupted");

  if (broken) {
    // Interrupted is amber, not red. A sweep cut short by `make dev` reloading
    // is worth saying — it did not finish — but it is not the same news as one
    // that failed, and colouring both alike is how you learn to ignore the red.
    const failed = broken.state === "failed";
    return (
      <div className={clsx("card p-3", failed ? "border-bad/40" : "border-warn/40")} role="alert">
        <p className={clsx("flex items-center gap-2 text-sm font-medium", failed ? "text-bad" : "text-warn")}>
          <AlertTriangle className="size-4 shrink-0" aria-hidden />
          {TASK_NOUN[broken.task]} {broken.state} {since(broken.age_seconds)} ago
        </p>
        {/* Whole, not truncated. A timed-out scoring batch puts the command that
            recovers it — and the money already spent — in exactly this string. */}
        {broken.error && (
          <p className="mt-1 whitespace-pre-wrap text-sm text-dim">{broken.error}</p>
        )}
      </div>
    );
  }

  if (recent.length === 0) {
    return <p className="text-sm text-dim">Nothing has been discovered yet.</p>;
  }

  return (
    <p className="text-sm text-dim">
      {recent
        .map((run) => {
          const tally = counts(run);
          return `${TASK_NOUN[run.task].toLowerCase()} ${since(run.age_seconds)} ago${
            tally ? ` · ${tally}` : ""
          }`;
        })
        .join("  ·  ")}
    </p>
  );
}

export function Discovery({ className }: { className?: string }) {
  const { data } = useRuns();
  const start = useStartRun();
  const qc = useQueryClient();
  const [menu, setMenu] = useState(false);
  const watching = useRef<number | null>(null);

  const active = data?.active ?? null;
  // Both guards matter. `disabled` covers the ordinary case; the check inside
  // the handler covers a double-tap that fires twice before React re-renders,
  // which React Query does not deduplicate for mutations. The server's 409 is
  // still the authority — these two only keep it from being the common path.
  const busy = start.isPending || active !== null;

  useEffect(() => {
    if (!data) return;
    const activeId = data.active?.id ?? null;
    const previous = watching.current;
    if (activeId === previous) return;
    watching.current = activeId;
    if (previous === null) return; // opened mid-run, or nothing was going

    // /api/runs is one statement, so a snapshot that no longer shows a run as
    // active always shows it recorded. No polling for the result.
    const finished = [data.last.ingest, data.last.score].find((run) => run?.id === previous);
    for (const key of ["pipeline", "applications", "stats", "review"]) {
      void qc.invalidateQueries({ queryKey: [key] });
    }
    if (!finished) return;
    const tally = counts(finished);
    const title = `${TASK_NOUN[finished.task]} ${finished.state === "done" ? "finished" : finished.state}`;
    if (finished.state === "done") {
      toast.success(title, { description: tally || finished.progress?.message });
    } else {
      toast.error(title, { description: finished.error ?? undefined });
    }
  }, [data, qc]);

  function launch(pipeline: RunPipeline) {
    setMenu(false);
    if (busy) return;
    start.mutate(pipeline);
  }

  const waiting = data?.waiting_to_score ?? 0;

  return (
    <div className={clsx("grid gap-2", className)}>
      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          onClick={() => launch("ingest_score")}
          disabled={busy}
          loading={start.isPending}
          className="flex-1 sm:flex-none"
        >
          {!start.isPending && <RefreshCw className="size-4" aria-hidden />}
          {active ? "Running…" : "Find new jobs"}
        </Button>
        <Button
          onClick={() => setMenu(true)}
          disabled={busy}
          aria-label="Run one step only"
          className="px-3"
        >
          <ChevronDown className="size-4" aria-hidden />
        </Button>
      </div>

      {active ? <Progress run={active} labels={data?.phase_labels ?? {}} /> : data && <LastRun data={data} />}

      <Sheet open={menu} onOpenChange={setMenu} title="Run one step">
        <div className="grid gap-2">
          <button
            type="button"
            onClick={() => launch("ingest")}
            className="flex items-start gap-3 rounded-lg border border-line p-3 text-left hover:bg-panel-2"
          >
            <Search className="mt-0.5 size-4 shrink-0 text-dim" aria-hidden />
            <span>
              <span className="font-medium">Ingest only</span>
              <span className="block text-sm text-dim">
                Find postings without scoring them. They wait in the queue until something
                scores them, so this is for checking discovery, not for a normal run.
              </span>
            </span>
          </button>
          <button
            type="button"
            onClick={() => launch("score")}
            disabled={waiting === 0}
            className="flex items-start gap-3 rounded-lg border border-line p-3 text-left hover:bg-panel-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Sparkles className="mt-0.5 size-4 shrink-0 text-dim" aria-hidden />
            <span>
              <span className="font-medium">
                Score only{waiting > 0 && ` — ${waiting} waiting`}
              </span>
              <span className="block text-sm text-dim">
                {waiting === 0
                  ? "Nothing is waiting to be scored."
                  : "Prefilter, then one Batch API job over everything already found. This one costs money."}
              </span>
            </span>
          </button>
        </div>
      </Sheet>
    </div>
  );
}
