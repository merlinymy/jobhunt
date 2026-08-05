import { useFill } from "../api/queries";
import type { DateForms, FillEntry } from "../api/types";
import { CopyButton } from "../components/CopyButton";
import { Card, ErrorState, Spinner } from "../components/ui";

/** The pain this exists for: an ATS makes you retype every role into separate
 *  Company / Title / Start / End / Description fields, and the same information
 *  is already on the resume it just accepted. That is most of the ten minutes.
 *
 *  Not autofill and deliberately not a browser extension — see
 *  docs/architecture.md, "Why no browser automation". */
export default function Fill() {
  const { data, isPending, error, refetch } = useFill();
  if (isPending) return <Spinner label="Loading your details" />;
  if (error) return <ErrorState error={error} retry={() => void refetch()} />;
  if (!data) return null;

  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="text-2xl font-semibold tracking-tight">Fill helper</h1>
      <p className="mt-1 mb-5 text-sm text-dim">
        Every field an ATS asks for, in its own box. Tap to copy. Identical for every
        application, so it does not depend on a packet.
      </p>

      <Section title="Identity">
        <div className="grid gap-2 sm:grid-cols-2">
          {data.identity.map((field) => (
            <Row key={field.label} label={field.label} value={field.value} />
          ))}
        </div>
      </Section>

      <Section title="Experience">
        {data.experiences.map((entry, i) => (
          <Entry key={i} entry={entry} nameLabel="Employer" />
        ))}
      </Section>

      <Section title="Projects">
        {data.projects.map((entry, i) => (
          <Entry key={i} entry={entry} nameLabel="Project" />
        ))}
      </Section>

      <Section title="Education">
        {data.education.map((entry, i) => (
          <Card key={i} className="mb-2">
            <div className="grid gap-2 sm:grid-cols-2">
              <Row label="School" value={entry.school} />
              <Row label="Degree" value={entry.degree} />
              <Row label="Field" value={entry.field} />
            </div>
            <Dates start={entry.start} end={entry.end} />
          </Card>
        ))}
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-6">
      <h2 className="mb-2 text-lg font-semibold tracking-tight">{title}</h2>
      {children}
    </section>
  );
}

function Row({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="min-w-0">
      <p className="text-xs text-dim">{label}</p>
      <CopyButton value={value} block className="-ml-2" />
    </div>
  );
}

function Entry({ entry, nameLabel }: { entry: FillEntry; nameLabel: string }) {
  return (
    <Card className="mb-2">
      <div className="grid gap-2 sm:grid-cols-2">
        <Row label={nameLabel} value={entry.company ?? entry.name} />
        <Row label="Title" value={entry.title ?? entry.role} />
        {entry.location && <Row label="Location" value={entry.location} />}
        {entry.url && <Row label="URL" value={entry.url} />}
      </div>
      <Dates start={entry.start} end={entry.end} />
      <div className="mt-3">
        <p className="text-xs text-dim">Description</p>
        <CopyButton value={entry.description} block className="-ml-2">
          <span className="whitespace-pre-wrap text-sm">{entry.description}</span>
        </CopyButton>
      </div>
    </Card>
  );
}

/** Every shape an ATS asks for, because they disagree: Workday wants a month
 *  name or number plus the year separately, Greenhouse takes MM/YYYY. A wrapping
 *  chip group beats the old five-column row. */
function Dates({ start, end }: { start: DateForms | null; end: DateForms | null }) {
  if (!start && !end) return null;
  return (
    <div className="mt-3 grid gap-2 sm:grid-cols-2">
      {([["Start", start], ["End", end]] as const).map(([label, forms]) => (
        <div key={label}>
          <p className="text-xs text-dim">{label}</p>
          {forms ? (
            <div className="flex flex-wrap gap-1">
              {[forms.iso, forms.slash, forms.month_name, forms.month, forms.year].map((v) => (
                <CopyButton key={v} value={v} className="border-line text-sm" />
              ))}
            </div>
          ) : (
            <p className="px-2 py-1.5 text-sm text-dim">Present</p>
          )}
        </div>
      ))}
    </div>
  );
}
