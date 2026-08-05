import { AlertTriangle, Download, ExternalLink, Handshake, Wand2 } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useBuildPacket, useGenerateAnswer, useSetAnswer } from "../api/mutations";
import { usePacket } from "../api/queries";
import type { Answer, DiffRow } from "../api/types";
import { CopyButton } from "../components/CopyButton";
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
              /* Disabled while pending is not cosmetic: a second click here is a
                 second billed model call. hx-disabled-elt used to do this free. */
              loading={build.isPending}
              disabled={build.isPending || !data.has_jd}
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
