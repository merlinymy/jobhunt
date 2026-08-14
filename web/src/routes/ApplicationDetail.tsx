import { ExternalLink } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useAddNote, useHonesty, useTransition } from "../api/mutations";
import { useApplication } from "../api/queries";
import { Button, Card, ErrorState, Pill, Spinner, inputClass } from "../components/ui";
import { comp, dateOnly, label } from "../lib/format";
import { stateTone } from "../lib/states";

export default function ApplicationDetail() {
  const id = Number(useParams().id);
  const { data, isPending, error, refetch } = useApplication(id);
  const transition = useTransition(id);
  const note = useAddNote(id);
  const honesty = useHonesty(id);
  const [text, setText] = useState("");

  if (isPending) return <Spinner />;
  if (error) return <ErrorState error={error} retry={() => void refetch()} />;
  if (!data) return null;
  const { app, events, referrals } = data;
  const band = comp(app.comp_min, app.comp_max);

  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-semibold tracking-tight">{app.title}</h1>
      <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-dim">
        <span>{app.company_name}</span>
        {app.location && <span>· {app.location}</span>}
        {band && <span>· {band}</span>}
        <Pill tone={stateTone(app.state)}>{label(app.state)}</Pill>
      </p>
      <div className="mt-3 flex flex-wrap gap-3 text-sm">
        <a href={app.apply_url} target="_blank" rel="noopener"
           className="inline-flex items-center gap-1 text-accent hover:underline">
          posting <ExternalLink className="size-3.5" aria-hidden />
        </a>
        {(app.state === "job_approved" || app.state === "packet_ready") && (
          <Link to={`/packet/${id}`} className="text-accent hover:underline">packet</Link>
        )}
      </div>

      <Card className="mt-4">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
          {([["Applied", dateOnly(app.applied_at)],
             ["First response", dateOnly(app.first_response_at)],
             ["ATS", app.ats_type ?? "—"],
             ["Source", app.source ?? "—"]] as const).map(([k, v]) => (
            <div key={k} className="flex justify-between gap-2">
              <dt className="text-dim">{k}</dt>
              <dd className="tabular">{v}</dd>
            </div>
          ))}
        </dl>
      </Card>

      {referrals.length > 0 && (
        <Card className="mt-3">
          <p className="text-sm font-medium">You know someone here</p>
          <ul className="mt-1.5 grid gap-1 text-sm text-dim">
            {referrals.map((c) => (
              <li key={c.id}>{c.name}{c.relationship ? ` · ${c.relationship}` : ""}</li>
            ))}
          </ul>
        </Card>
      )}

      {/* Read this as the primary metric, not a footnote — CLAUDE.md. */}
      <Card className="mt-3">
        <p className="text-sm font-medium">Would you have applied anyway?</p>
        <p className="mt-0.5 text-xs text-dim">
          The check that this has not quietly become a volume machine.
        </p>
        <div className="mt-3 flex gap-2">
          {([[1, "Yes"], [0, "No"]] as const).map(([value, text]) => (
            <Button
              key={value}
              variant={app.would_apply_anyway === value ? "primary" : "outline"}
              size="sm"
              disabled={honesty.isPending}
              onClick={() => honesty.mutate(value)}
            >
              {text}
            </Button>
          ))}
        </div>
      </Card>

      {app.next_states.length > 0 && (
        <Card className="mt-3">
          <p className="text-sm font-medium">Move to</p>
          <div className="mt-2 flex flex-wrap gap-2">
            {app.next_states.map((state) => (
              <Button
                key={state}
                size="sm"
                disabled={transition.isPending}
                onClick={() => transition.mutate({ to_state: state })}
              >
                {label(state)}
              </Button>
            ))}
          </div>
        </Card>
      )}

      <Card className="mt-3">
        <p className="text-sm font-medium">History</p>
        <ol className="mt-2 grid gap-2 text-sm">
          {events.map((event) => (
            <li key={event.id} className="flex gap-3">
              <span className="shrink-0 text-dim tabular">{dateOnly(event.occurred_at)}</span>
              <span className="min-w-0">
                <span className="font-medium">
                  {event.to_state ? label(event.to_state) : event.kind}
                </span>
                {event.detail && <span className="text-dim"> — {event.detail}</span>}
              </span>
            </li>
          ))}
        </ol>
        <form
          className="mt-3 flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) { note.mutate(text); setText(""); }
          }}
        >
          <input className={inputClass} value={text} placeholder="Add a note…"
                 onChange={(e) => setText(e.target.value)} />
          <Button type="submit" size="sm" loading={note.isPending} disabled={!text.trim()}>
            Add
          </Button>
        </form>
      </Card>
    </div>
  );
}
