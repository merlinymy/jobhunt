import {
  AlertTriangle,
  Download,
  ExternalLink,
  Handshake,
  Loader2,
  ShieldCheck,
  Wand2,
} from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useBuildPacket, useGenerateAnswer, useSetAnswer } from "../api/mutations";
import { usePacket } from "../api/queries";
import type { Answer, DiffRow, Finding, Run } from "../api/types";
import { CopyButton } from "../components/CopyButton";
import { FormAnswers } from "../components/FormAnswers";
import { Gaps } from "../components/Gaps";
import { PacketChat } from "../components/PacketChat";
import { ResumeText } from "../components/ResumeText";
import { Button, Card, ErrorState, Pill, Spinner, inputClass } from "../components/ui";
import { label } from "../lib/format";
import { stateTone } from "../lib/states";

export default function Packet() {
  const id = Number(useParams().id);
  const { data, isPending, error, refetch } = usePacket(id);
  const build = useBuildPacket(id);
  const [showDiff, setShowDiff] = useState(false);

  if (isPending) return <Spinner label="Loading the packet" />;
  if (error) return <ErrorState error={error} retry={() => void refetch()} />;
  if (!data) return null;

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-4">
        <h1 className="text-2xl font-semibold tracking-tight">{data.app.title}</h1>
        <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-dim">
          <span>{data.app.company_name}</span>
          <span>·</span>
          <span>{data.where}</span>
          <Pill tone={stateTone(data.app.state)}>{label(data.app.state)}</Pill>
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <a
            href={data.apply_url}
            target="_blank"
            rel="noopener"
            className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline"
          >
            Open the posting <ExternalLink className="size-3.5" aria-hidden />
          </a>
          <Link to={`/applications/${id}`} className="text-sm text-dim hover:text-accent">
            details
          </Link>
        </div>
      </div>

      {data.referral && (
        <p className="mb-4 flex items-center gap-2 rounded-lg bg-good/10 px-3 py-2 text-sm text-good">
          <Handshake className="size-4 shrink-0" aria-hidden />
          You know <strong>{data.referral}</strong> here — ping first.
        </p>
      )}

      {data.error && (
        <Card className="mb-4 border-bad/40">
          <p className="flex items-center gap-2 font-medium text-bad">
            <AlertTriangle className="size-4" aria-hidden /> The packet was not built
          </p>
          <pre className="mt-2 whitespace-pre-wrap font-sans text-sm text-dim">{data.error}</pre>
        </Card>
      )}

      {data.duplicates > 0 && (
        <Card className="mb-4">
          <p className="text-sm">
            The same role is listed in {data.duplicates} other place
            {data.duplicates === 1 ? "" : "s"}. Apply to one.
          </p>
          <ul className="mt-2 grid gap-1 text-sm text-dim">
            {data.duplicate_rows.map((row) => (
              <li key={row.id}>
                <Link to={`/packet/${row.id}`} className="text-accent hover:underline">
                  #{row.id}
                </Link>{" "}
                {row.location ?? "—"} · {label(row.state)}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {/* Resume first as an artifact to grab, then the answers, which is where
          the ten minutes actually goes. The diff is secondary and collapsed —
          inverted from the old layout, which led with it. */}
      <Card className="mb-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="font-medium">Resume</p>
            <p className="mt-0.5 text-sm text-dim">
              {data.pdf
                ? `${data.pdf.bullets} bullets, ${data.pdf.reworded} reworded`
                : "Not built yet."}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {data.pdf && (
              <a
                href={`/api/packet/${id}/resume.pdf`}
                target="_blank"
                rel="noopener"
                className="tap inline-flex items-center gap-2 rounded-lg border border-line px-4 text-[15px] hover:bg-panel-2"
              >
                <Download className="size-4" aria-hidden /> PDF
              </a>
            )}
            <Button
              variant="primary"
              /* Disabled while a build is going is not cosmetic: a second click
                 is a second pair of billed model calls. The guard has to read
                 `data.run` and not just `isPending` — the request now returns a
                 202 after about a tenth of a second, so `isPending` goes false
                 while the build still has a minute left to run. The database
                 lock would turn a second click into a 409 rather than a second
                 bill, but a button that invites the click is still wrong. */
              loading={build.isPending || !!data.run}
              disabled={build.isPending || !!data.run || !data.has_jd}
              onClick={() => build.mutate()}
              title={data.has_jd ? undefined : "This posting has no job description"}
            >
              <Wand2 className="size-4" aria-hidden />
              {data.pdf ? "Rebuild" : "Build packet"}
            </Button>
          </div>
        </div>

        {data.diff.length > 0 && (
          <div className="mt-4 border-t border-line pt-3">
            <button
              className="text-sm text-dim hover:text-accent"
              onClick={() => setShowDiff((v) => !v)}
              aria-expanded={showDiff}
            >
              {showDiff ? "Hide" : "Show"} what changed ({data.diff.length})
            </button>
            {showDiff && <Diff rows={data.diff} />}
          </div>
        )}
      </Card>

      <BuildProgress run={data.run} lastRun={data.last_run} labels={data.phase_labels} />

      {!data.run && (
        <>
          <Findings findings={data.findings} />
          {/* Text before chat, deliberately: you read the resume, then argue
              with it. The line numbers here are the ones the model is shown,
              so a complaint can name one. */}
          <ResumeText data={data.resume_text} />
          <Gaps gaps={data.gaps} analysed={data.gaps_analysed} />
          <FormAnswers id={id} answers={data.form_answers} />
          {data.resume_text && (
            <PacketChat id={id} messages={data.messages} disabled={!!data.run} />
          )}
        </>
      )}

      <h2 className="mb-2 text-lg font-semibold tracking-tight">
        Answers
        {data.unknowns > 0 && (
          <span className="ml-2 align-middle">
            <Pill tone="text-warn">{data.unknowns} unknown</Pill>
          </span>
        )}
      </h2>
      <div className="grid gap-3 xl:grid-cols-2">
        {data.answers.map((answer) => (
          <AnswerCard key={answer.key} id={id} answer={answer} />
        ))}
      </div>
    </div>
  );
}

/** The four steps of a build, in order, so the page can show where it is even
 *  before the worker has reported the first one. Mirrors `runs.PHASE_LABELS`;
 *  the labels themselves come from the server so the wording lives in one file. */
const BUILD_PHASES = ["selecting", "checking", "rendering", "finished"] as const;

/** What the build is doing right now.
 *
 *  This exists because the build stopped being a blocking request. Two model
 *  calls back to back is a long time to look at a spinner that cannot say which
 *  one it is on — and "checking every claim against your corpus" is the step
 *  people assume has hung, because nothing about a resume suggests a second
 *  model is reading it.
 *
 *  Also shows a *finished* run when it failed. A build that died while the tab
 *  was closed otherwise leaves the page looking like it was never clicked. */
function BuildProgress({
  run,
  lastRun,
  labels,
}: {
  run: Run | null;
  lastRun: Run | null;
  labels: Record<string, string>;
}) {
  if (!run) {
    if (lastRun && (lastRun.state === "failed" || lastRun.state === "interrupted")) {
      return (
        <Card className="mb-4 border-bad/40">
          <p className="flex items-center gap-2 font-medium text-bad">
            <AlertTriangle className="size-4 shrink-0" aria-hidden />
            The last build {lastRun.state === "failed" ? "failed" : "was interrupted"}
          </p>
          {lastRun.error && (
            <pre className="mt-2 whitespace-pre-wrap font-sans text-sm text-dim">
              {lastRun.error}
            </pre>
          )}
        </Card>
      );
    }
    return null;
  }

  const phase = run.progress?.phase ?? "selecting";
  const at = BUILD_PHASES.indexOf(phase as (typeof BUILD_PHASES)[number]);
  return (
    <Card className="mb-4 border-accent/40">
      <p className="flex items-center gap-2 font-medium">
        <Loader2 className="size-4 shrink-0 animate-spin text-accent" aria-hidden />
        {labels[phase] ?? "Working"}
      </p>
      {run.progress?.message && (
        <p className="mt-1 text-sm text-dim">{run.progress.message}</p>
      )}
      <ol className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-sm">
        {BUILD_PHASES.slice(0, 3).map((p, i) => (
          <li
            key={p}
            className={
              // Done, current, not yet. Three states rather than a bar, because
              // neither model call has a denominator to fill one with.
              at > i ? "text-good" : at === i ? "text-ink" : "text-dim/60"
            }
          >
            {at > i ? "✓ " : at === i ? "› " : "· "}
            {labels[p] ?? p}
          </li>
        ))}
      </ol>
      {run.task === "packet" && run.progress?.total ? (
        <p className="mt-2 text-sm text-dim">
          Building {run.progress.done} of {run.progress.total} — this is the queued
          batch, not just this one.
        </p>
      ) : null}
    </Card>
  );
}

/** Labels for what a check objected to. Kept short: the message says the rest,
 *  and the badge is there to make a list of five scannable in one pass. */
const FINDING_LABEL: Record<Finding["kind"], string> = {
  number: "number",
  identifier: "identifier",
  homoglyph: "lookalike letter",
  unknown: "not in corpus",
  duplicate: "duplicate",
  review: "unsupported claim",
  unchecked: "not checked",
};

/** What the checks objected to, beside the resume rather than instead of it.
 *
 *  Three states, and conflating any two of them loses the point:
 *    null  — never built. Say nothing; there is no resume to have opinions about.
 *    []    — built and every check passed. Worth stating positively, because it
 *            is the only thing that distinguishes a clean resume from an
 *            unchecked one, and those now look identical in the PDF.
 *    [...] — flagged. Never hidden behind a disclosure: the whole reason these
 *            stopped blocking is that a human reads them, and a warning folded
 *            into a collapsed panel is a warning nobody reads.
 */
function Findings({ findings }: { findings: Finding[] | null }) {
  if (findings === null) return null;

  if (findings.length === 0) {
    return (
      <Card className="mb-4 border-good/30">
        <p className="flex items-center gap-2 text-sm text-good">
          <ShieldCheck className="size-4 shrink-0" aria-hidden />
          Every line traces back to the corpus.
        </p>
      </Card>
    );
  }

  const dropped = findings.filter((f) => f.blocking).length;
  return (
    <Card className="mb-4 border-warn/40">
      <p className="flex items-center gap-2 font-medium text-warn">
        <AlertTriangle className="size-4 shrink-0" aria-hidden />
        {findings.length} thing{findings.length === 1 ? "" : "s"} to look at before you
        send this
      </p>
      <p className="mt-1 text-sm text-dim">
        The packet was built anyway — you read it before it goes out, so these are
        notes, not a refusal.
        {dropped > 0 &&
          ` ${dropped} line${dropped === 1 ? " was" : "s were"} dropped and ${
            dropped === 1 ? "is" : "are"
          } not in the PDF.`}
      </p>
      <ul className="mt-3 space-y-3">
        {findings.map((f, i) => (
          <li key={i} className="border-t border-line pt-3 first:border-0 first:pt-0">
            <div className="flex flex-wrap items-center gap-2">
              <Pill tone={f.blocking ? "text-bad" : "text-warn"}>
                {FINDING_LABEL[f.kind] ?? f.kind}
              </Pill>
              <span className="text-sm text-dim">{f.where}</span>
            </div>
            <p className="mt-1 text-[15px]">{f.message}</p>
            {f.source && (
              <p className="mt-1 text-sm text-dim">
                <span className="text-ink">Corpus says:</span> {f.source}
              </p>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** One card per question. The old `table.answers` gave its label column 19rem,
 *  which on a 390px phone left about 86px for the answer — not a CSS bug so much
 *  as the wrong element for the data. */
function AnswerCard({ id, answer }: { id: number; answer: Answer }) {
  const generate = useGenerateAnswer(id);
  const set = useSetAnswer(id);
  const [draft, setDraft] = useState("");
  const narrative = answer.tier === "narrative";

  return (
    <Card>
      <p className="text-sm font-medium">{answer.text}</p>

      {answer.answer ? (
        <div className="mt-2">
          <CopyButton value={answer.answer} block className="border-line bg-panel-2">
            <span className="whitespace-pre-wrap text-sm">{answer.answer}</span>
          </CopyButton>
        </div>
      ) : (
        <p className="mt-2 text-sm text-dim">
          {answer.optional ? "Left blank deliberately." : "No answer yet."}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-xs text-dim">
          {answer.tier}
          {answer.source ? ` · ${answer.source}` : ""}
        </span>
        <span className="flex-1" />
        {narrative && (
          <Button
            size="sm"
            loading={generate.isPending}
            disabled={generate.isPending}
            onClick={() => generate.mutate(answer.key)}
          >
            {answer.answer ? "Redraft" : "Draft"}
          </Button>
        )}
      </div>

      {!narrative && answer.missing && (
        <form
          className="mt-3 flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (draft.trim()) set.mutate({ key: answer.key, answer: draft });
          }}
        >
          <input
            className={inputClass}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Type it once; reused for this company"
          />
          <Button type="submit" size="sm" loading={set.isPending} disabled={!draft.trim()}>
            Save
          </Button>
        </form>
      )}
    </Card>
  );
}

/** Two columns on a wide screen; stacked below it. A diff should never scroll
 *  sideways — that is the one shape where losing the left edge loses the point. */
function Diff({ rows }: { rows: DiffRow[] }) {
  return (
    <ul className="mt-3 grid gap-3">
      {rows.map((row, index) => (
        <li key={index} className="grid gap-1 text-sm md:grid-cols-2 md:gap-4">
          <p className="text-dim line-through decoration-bad/50">{row.before}</p>
          <p>{row.after}</p>
        </li>
      ))}
    </ul>
  );
}
