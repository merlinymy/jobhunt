import { useSearchParams } from "react-router-dom";
import { useStats } from "../api/queries";
import type { Bucket, StatsTable } from "../api/types";
import { SortableHeader } from "../components/SortableHeader";
import { Card, ErrorState, Spinner } from "../components/ui";
import { pct } from "../lib/format";
import { useIsDesktop } from "../lib/useMediaQuery";

export default function Stats() {
  const [params, setParams] = useSearchParams();
  const query = Object.fromEntries(params.entries());
  const { data, isPending, error, refetch } = useStats(query);
  const desktop = useIsDesktop();

  if (isPending) return <Spinner label="Loading stats" />;
  if (error) return <ErrorState error={error} retry={() => void refetch()} />;
  if (!data) return null;

  function sort(prefix: string, column: string, direction: "asc" | "desc") {
    setParams({ ...query, [`${prefix}_sort`]: column, [`${prefix}_dir`]: direction });
  }

  return (
    <div className="mx-auto max-w-7xl">
      <h1 className="mb-4 text-2xl font-semibold tracking-tight">Stats</h1>

      {/* Interview rate leads, and the honesty ratio sits beside it. CLAUDE.md:
          a falling ratio means the system is manufacturing volume, which is the
          exact thing this design exists to prevent. */}
      <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Big label="Interview rate" value={pct(data.overall.interview_rate)}
             hint={`${data.overall.interviews} of ${data.overall.applied} applications`} />
        <Big label="Would apply anyway" value={pct(data.honesty.ratio)}
             hint={`${data.honesty.yes} of ${data.honesty.answered} answered`} />
        <Big label="Response rate" value={pct(data.overall.response_rate)}
             hint={`${data.overall.responded} replied`} />
        <Big label="LLM spend" value={`$${data.spend.total.toFixed(2)}`}
             hint={`${data.spend.by_task.reduce((n, t) => n + t.calls, 0)} calls`} />
      </div>

      {Object.entries(data.tables).map(([prefix, table]) => (
        <section key={prefix} className="mb-6">
          <h2 className="mb-2 text-lg font-semibold tracking-tight capitalize">{prefix}</h2>
          {desktop ? (
            <Table table={table} onSort={(c, d) => sort(prefix, c, d)} />
          ) : (
            <BucketCards rows={table.rows} />
          )}
        </section>
      ))}
    </div>
  );
}

function Big({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Card>
      <p className="text-sm text-dim">{label}</p>
      <p className="mt-1 text-3xl font-semibold tracking-tight tabular">{value}</p>
      {hint && <p className="mt-0.5 text-xs text-dim">{hint}</p>}
    </Card>
  );
}

function Table({
  table, onSort,
}: { table: StatsTable; onSort: (column: string, direction: "asc" | "desc") => void }) {
  return (
    <div className="card overflow-x-auto">
      <table className="w-full min-w-[46rem] text-sm">
        <thead>
          <tr>
            {table.headers.map((h) => (
              <SortableHeader key={h.column} header={h} onSort={onSort} />
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row) => (
            <tr key={row.label} className="border-b border-line/60 last:border-0">
              {/* Sticky first column: the fix for the four tables that used to
                  scroll their own row labels off the left edge. */}
              <td className="sticky-label px-3 py-2 font-medium">{row.label}</td>
              <Num v={row.applied} /><Num v={row.responded} /><Num v={row.interviews} />
              <Num v={row.offers} /><Num v={row.rejected} /><Num v={row.pending} />
              <Num v={pct(row.response_rate)} /><Num v={pct(row.interview_rate)} />
              <Num v={pct(row.offer_rate)} />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Num({ v }: { v: string | number }) {
  return <td className="px-3 py-2 text-right tabular">{v}</td>;
}

function BucketCards({ rows }: { rows: Bucket[] }) {
  return (
    <div className="grid gap-2">
      {rows.map((row) => (
        <details key={row.label} className="card p-3">
          <summary className="flex cursor-pointer items-baseline justify-between gap-3">
            <span className="font-medium">{row.label}</span>
            <span className="text-sm text-dim tabular">
              {row.applied} applied · {row.interviews} int · {pct(row.interview_rate)}
            </span>
          </summary>
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
            {([["Responded", row.responded], ["Offers", row.offers],
               ["Rejected", row.rejected], ["Pending", row.pending],
               ["Response rate", pct(row.response_rate)],
               ["Offer rate", pct(row.offer_rate)]] as const).map(([k, v]) => (
              <div key={k} className="flex justify-between gap-2">
                <dt className="text-dim">{k}</dt>
                <dd className="tabular">{v}</dd>
              </div>
            ))}
          </dl>
        </details>
      ))}
    </div>
  );
}
