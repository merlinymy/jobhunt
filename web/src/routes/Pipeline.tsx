import clsx from "clsx";
import { ChevronRight, SlidersHorizontal, X } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { usePipeline } from "../api/queries";
import type { Application, Pipeline as PipelineData } from "../api/types";
import { Discovery } from "../components/Discovery";
import { Sheet } from "../components/Sheet";
import { SortableHeader } from "../components/SortableHeader";
import { Button, Card, EmptyState, ErrorState, Pill, Spinner, inputClass } from "../components/ui";
import { dateOnly, label, pct } from "../lib/format";
import { stateTone } from "../lib/states";
import { useIsDesktop } from "../lib/useMediaQuery";

const FILTERS = [
  { name: "state", label: "State", facet: "state" },
  { name: "ats", label: "ATS", facet: "ats" },
  { name: "source", label: "Source", facet: "source" },
] as const;

export default function Pipeline() {
  const [params, setParams] = useSearchParams();
  const query = Object.fromEntries(params.entries());
  const { data, isPending, error, refetch } = usePipeline(query);
  const desktop = useIsDesktop();
  const [filtersOpen, setFiltersOpen] = useState(false);

  /* Filter and sort live in the URL, not in state. README sells "any view is a
     bookmarkable URL that survives a reload", and it is the property most easily
     lost in a port like this. `replace` on keystrokes so back does not collect
     thirty entries. */
  function setParam(name: string, value: string, replace = false) {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value);
    else next.delete(name);
    setParams(next, { replace });
  }

  if (isPending) return <Spinner label="Loading the pipeline" />;
  if (error) return <ErrorState error={error} retry={() => void refetch()} />;
  if (!data) return null;

  const active = FILTERS.filter((f) => query[f.name]).length + (query.q ? 1 : 0);

  return (
    <div className="mx-auto max-w-7xl">
      {/* Discovery sits above the funnel because it is what fills it, and
          because "why has nothing arrived since Tuesday" is the question this
          page is opened with. */}
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Pipeline</h1>
        <Discovery className="sm:w-96" />
      </div>

      <Panels data={data} />

      <Funnel data={data} current={query.state ?? ""} onPick={(s) => setParam("state", s)} />

      <div className="mb-3 flex items-center gap-2">
        <input
          className={inputClass}
          placeholder="Search company or title…"
          defaultValue={query.q ?? ""}
          onChange={(e) => setParam("q", e.target.value, true)}
          aria-label="Search"
        />
        {!desktop && (
          <Button onClick={() => setFiltersOpen(true)} className="shrink-0">
            <SlidersHorizontal className="size-4" aria-hidden />
            {active > 0 && <span className="tabular">{active}</span>}
          </Button>
        )}
        {desktop &&
          FILTERS.map((f) => (
            <select
              key={f.name}
              aria-label={f.label}
              className={clsx(inputClass, "w-40")}
              value={query[f.name] ?? ""}
              onChange={(e) => setParam(f.name, e.target.value)}
            >
              <option value="">{f.label}: any</option>
              {(data.facets[f.facet] ?? []).map((v) => (
                <option key={v} value={v}>
                  {label(v)}
                </option>
              ))}
            </select>
          ))}
      </div>

      {active > 0 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          {[...FILTERS.map((f) => f.name), "q"].map((name) =>
            query[name] ? (
              <button
                key={name}
                onClick={() => setParam(name, "")}
                className="inline-flex items-center gap-1 rounded-full border border-line px-2.5 py-1 text-xs hover:bg-panel-2"
              >
                {name}: {label(query[name])}
                <X className="size-3" aria-hidden />
              </button>
            ) : null,
          )}
        </div>
      )}

      {data.applications.length === 0 ? (
        <EmptyState title="Nothing matches those filters." />
      ) : desktop ? (
        <Table data={data} onSort={(c, d) => setParams({ ...query, sort: c, dir: d })} />
      ) : (
        <Cards rows={data.applications} />
      )}

      <p className="mt-3 text-sm text-dim">
        {data.applications.length} row{data.applications.length === 1 ? "" : "s"}
      </p>

      <Sheet open={filtersOpen} onOpenChange={setFiltersOpen} title="Filters">
        <div className="grid gap-3">
          {FILTERS.map((f) => (
            <label key={f.name} className="grid gap-1.5">
              <span className="text-sm font-medium">{f.label}</span>
              {/* Native select on purpose: on iOS this is the wheel picker, with
                  a free 44px target and behaviour nobody has to re-implement. */}
              <select
                className={inputClass}
                value={query[f.name] ?? ""}
                onChange={(e) => setParam(f.name, e.target.value)}
              >
                <option value="">Any</option>
                {(data.facets[f.facet] ?? []).map((v) => (
                  <option key={v} value={v}>
                    {label(v)}
                  </option>
                ))}
              </select>
            </label>
          ))}
          <Button variant="primary" onClick={() => setFiltersOpen(false)}>
            Done
          </Button>
        </div>
      </Sheet>
    </div>
  );
}

function Panels({ data }: { data: PipelineData }) {
  return (
    <div className="mb-4 grid gap-3 sm:grid-cols-3">
      <Card>
        <p className="text-sm text-dim">Would apply anyway</p>
        <p className="mt-1 text-2xl font-semibold tabular">{pct(data.honesty.ratio)}</p>
        <p className="mt-0.5 text-xs text-dim">
          {data.honesty.yes} of {data.honesty.answered} answered
        </p>
      </Card>
      <Card>
        <p className="text-sm text-dim">Approved backlog</p>
        <p className="mt-1 text-2xl font-semibold tabular">{data.health.approved_backlog}</p>
        <p className="mt-0.5 text-xs text-dim">
          {data.health.oldest_days === null
            ? "nothing waiting"
            : `oldest ${data.health.oldest_days}d`}
        </p>
      </Card>
      <Card>
        <p className="text-sm text-dim">Applied this month</p>
        <p className="mt-1 text-2xl font-semibold tabular">{data.health.applied_this_month}</p>
      </Card>
    </div>
  );
}

/** Eleven boxes stacked on a phone is not a funnel. One snap-scrolling chip row. */
function Funnel({
  data, current, onPick,
}: { data: PipelineData; current: string; onPick: (state: string) => void }) {
  return (
    <div className="-mx-4 mb-3 flex snap-x gap-1.5 overflow-x-auto px-4 pb-1 sm:mx-0 sm:flex-wrap sm:px-0">
      {Object.keys(data.funnel).map((state) => (
        <button
          key={state}
          onClick={() => onPick(current === state ? "" : state)}
          className={clsx(
            "shrink-0 snap-start rounded-lg border px-3 py-1.5 text-sm whitespace-nowrap",
            current === state
              ? "border-accent bg-accent/10 text-accent"
              : "border-line text-dim hover:text-ink",
          )}
        >
          {label(state)} <span className="tabular">{data.counts[state] ?? 0}</span>
        </button>
      ))}
    </div>
  );
}

function Table({
  data, onSort,
}: { data: PipelineData; onSort: (column: string, direction: "asc" | "desc") => void }) {
  return (
    <div className="card overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr>
            {data.headers.map((h) => (
              <SortableHeader key={h.column} header={h} onSort={onSort} />
            ))}
            <th className="border-b border-line" />
          </tr>
        </thead>
        <tbody>
          {data.applications.map((row) => (
            <tr key={row.id} className="border-b border-line/60 last:border-0 hover:bg-panel-2">
              {/* Sticky, so scrolling right does not scroll the meaning away. */}
              <td className="sticky-label px-3 py-2 font-medium">{row.company_name}</td>
              <td className="px-3 py-2">{row.title}</td>
              <td className="px-3 py-2">
                <Pill tone={stateTone(row.state)}>{label(row.state)}</Pill>
              </td>
              <td className="px-3 py-2 text-dim">{row.ats_type ?? "—"}</td>
              <td className="px-3 py-2 text-dim">{row.source ?? "—"}</td>
              <td className="px-3 py-2 text-dim">{dateOnly(row.applied_at)}</td>
              <td className="px-3 py-2 text-dim">{row.referral_contact_id ? "yes" : "—"}</td>
              <td className="px-3 py-2 text-dim">
                {row.would_apply_anyway === null ? "—" : row.would_apply_anyway ? "yes" : "no"}
              </td>
              <td className="px-3 py-2 text-right whitespace-nowrap">
                <RowLinks row={row} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Cards({ rows }: { rows: Application[] }) {
  return (
    <div className="grid gap-2">
      {rows.map((row) => (
        <Link
          key={row.id}
          to={`/applications/${row.id}`}
          className="card flex items-center gap-3 p-3 hover:bg-panel-2"
        >
          <div className="min-w-0 flex-1">
            <p className="truncate font-medium">
              {row.company_name} — {row.title}
            </p>
            <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-dim">
              <Pill tone={stateTone(row.state)}>{label(row.state)}</Pill>
              <span>{dateOnly(row.applied_at)}</span>
              {row.ats_type && <span>{row.ats_type}</span>}
            </p>
          </div>
          <ChevronRight className="size-4 shrink-0 text-dim" aria-hidden />
        </Link>
      ))}
    </div>
  );
}

function RowLinks({ row }: { row: Application }) {
  const packet = row.state === "job_approved" || row.state === "packet_ready";
  return (
    <span className="flex justify-end gap-3">
      {packet && (
        <Link to={`/packet/${row.id}`} className="text-accent hover:underline">
          packet
        </Link>
      )}
      <Link to={`/applications/${row.id}`} className="text-dim hover:text-accent">
        details
      </Link>
    </span>
  );
}
