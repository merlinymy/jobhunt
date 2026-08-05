import { useState } from "react";
import { useTailor } from "../api/mutations";
import { ApiError } from "../api/client";
import { Button, Card, Field, inputClass } from "../components/ui";

/** Paste a JD, get a tailored PDF plus a diff against the master resume.
 *  A validation failure renders the reason instead of a PDF: the diff is the
 *  gate, and a diff that cannot be produced blocks the packet. */
export default function Tailor() {
  const [jd, setJd] = useState("");
  const [limit, setLimit] = useState(10);
  const tailor = useTailor();
  const error = tailor.error instanceof ApiError ? tailor.error.message : null;

  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="mb-4 text-2xl font-semibold tracking-tight">Tailor</h1>

      <form
        className="grid gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          tailor.mutate({ jd_text: jd, limit });
        }}
      >
        <Field label="Job description">
          <textarea
            className={`${inputClass} min-h-56 py-2 font-mono text-sm`}
            value={jd}
            onChange={(e) => setJd(e.target.value)}
            placeholder="Paste the posting…"
          />
        </Field>
        <div className="flex items-end gap-3">
          <Field label="Bullets">
            <input type="number" min={1} max={30} value={limit}
                   onChange={(e) => setLimit(Number(e.target.value))}
                   className={`${inputClass} w-24`} />
          </Field>
          <Button type="submit" variant="primary" loading={tailor.isPending}
                  disabled={!jd.trim() || tailor.isPending}>
            Tailor
          </Button>
        </div>
      </form>

      {/* Inline, not a toast. This is the validator explaining exactly what it
          rejected, and it should stay on screen while you read it. */}
      {error && (
        <Card className="mt-4 border-bad/40">
          <p className="font-medium text-bad">Not tailored</p>
          <pre className="mt-2 whitespace-pre-wrap font-sans text-sm text-dim">{error}</pre>
        </Card>
      )}

      {tailor.data && (
        <div className="mt-4">
          <Card>
            <p className="font-medium">
              {tailor.data.kept} bullets, {tailor.data.reworded} reworded
            </p>
            <p className="mt-1 text-sm text-dim">{tailor.data.reasoning}</p>
            <a href={`/api/renders/${tailor.data.pdf}`} target="_blank" rel="noopener"
               className="mt-3 inline-block text-accent hover:underline">
              Download the PDF
            </a>
          </Card>
          <ul className="mt-3 grid gap-3">
            {tailor.data.diff.map((row, i) => (
              <li key={i} className="grid gap-1 text-sm md:grid-cols-2 md:gap-4">
                <p className="text-dim line-through decoration-bad/50">{row.before}</p>
                <p>{row.after}</p>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
