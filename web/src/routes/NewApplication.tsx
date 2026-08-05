import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { useCreateApplication } from "../api/mutations";
import { useContacts, useMeta, useUrlCheck } from "../api/queries";
import { Button, Card, Field, inputClass } from "../components/ui";
import { label } from "../lib/format";

/** Log an application submitted by hand. Seeds straight into `applied`. */
export default function NewApplication() {
  const navigate = useNavigate();
  const create = useCreateApplication();
  const { data: meta } = useMeta();
  const { data: contactData } = useContacts();
  const [form, setForm] = useState<Record<string, string>>({
    remote: "unknown",
    source: "manual",
    would_apply_anyway: "1",
  });
  const [debouncedUrl, setDebouncedUrl] = useState("");
  const set = (name: string, value: string) => setForm((f) => ({ ...f, [name]: value }));
  const check = useUrlCheck(debouncedUrl);
  const error = create.error instanceof ApiError ? create.error.message : null;

  // The old form used hx-trigger="keyup changed delay:600ms". Same idea; the
  // caching and request cancellation come free from the query layer.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedUrl(form.apply_url ?? ""), 500);
    return () => clearTimeout(timer);
  }, [form.apply_url]);

  useEffect(() => {
    if (!form.applied_at && meta?.today) set("applied_at", meta.today);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta?.today]);

  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="mb-4 text-2xl font-semibold tracking-tight">Log application</h1>

      {error && (
        <Card className="mb-4 border-bad/40">
          <p className="font-medium text-bad">Not saved</p>
          <p className="mt-1 text-sm text-dim">{error}</p>
        </Card>
      )}

      <form
        className="grid gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate(form, {
            onSuccess: ({ id }) => navigate(`/applications/${id}`),
          });
        }}
      >
        <Field label="Apply URL" hint={<UrlHint />}>
          <input
            className={inputClass}
            value={form.apply_url ?? ""}
            onChange={(e) => set("apply_url", e.target.value)}
            placeholder="https://boards.greenhouse.io/…"
            required
          />
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Company">
            <input className={inputClass} required value={form.company ?? ""}
                   onChange={(e) => set("company", e.target.value)} />
          </Field>
          <Field label="Title">
            <input className={inputClass} required value={form.title ?? ""}
                   onChange={(e) => set("title", e.target.value)} />
          </Field>
          <Field label="Location">
            <input className={inputClass} value={form.location ?? ""}
                   onChange={(e) => set("location", e.target.value)} />
          </Field>
          <Field label="Remote">
            <select className={inputClass} value={form.remote}
                    onChange={(e) => set("remote", e.target.value)}>
              {["unknown", "remote", "hybrid", "onsite"].map((v) => (
                <option key={v} value={v}>{v}</option>
              ))}
            </select>
          </Field>
          <Field label="Applied on">
            <input type="date" className={inputClass} value={form.applied_at ?? ""}
                   onChange={(e) => set("applied_at", e.target.value)} />
          </Field>
          <Field label="Source">
            <input className={inputClass} value={form.source}
                   onChange={(e) => set("source", e.target.value)} />
          </Field>
          <Field label="Comp min" hint="Neither filters nor ranks — it exists to answer with.">
            <input inputMode="numeric" className={inputClass} value={form.comp_min ?? ""}
                   onChange={(e) => set("comp_min", e.target.value)} />
          </Field>
          <Field label="Comp max">
            <input inputMode="numeric" className={inputClass} value={form.comp_max ?? ""}
                   onChange={(e) => set("comp_max", e.target.value)} />
          </Field>
        </div>

        <Field label="Referral">
          <select className={inputClass} value={form.referral_contact_id ?? ""}
                  onChange={(e) => set("referral_contact_id", e.target.value)}>
            <option value="">Nobody</option>
            {(contactData?.contacts ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}{c.company_name ? ` — ${c.company_name}` : ""}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Would you have applied anyway?"
          hint="The honesty check. A falling ratio means the system is manufacturing volume."
        >
          <select className={inputClass} value={form.would_apply_anyway}
                  onChange={(e) => set("would_apply_anyway", e.target.value)}>
            <option value="1">Yes</option>
            <option value="0">No</option>
          </select>
        </Field>

        <Field label="Known outcome" hint="Only if it is already decided.">
          <select className={inputClass} value={form.outcome ?? ""}
                  onChange={(e) => set("outcome", e.target.value)}>
            <option value="">Still open</option>
            {(meta?.transitions?.applied ?? ["rejected", "interview", "offer"])
              .filter((s) => s !== "expired")
              .map((s) => (
                <option key={s} value={s}>{label(s)}</option>
              ))}
          </select>
        </Field>

        <Field label="Note">
          <textarea className={`${inputClass} min-h-20 py-2`} value={form.note ?? ""}
                    onChange={(e) => set("note", e.target.value)} />
        </Field>

        <Field label="Job description" hint="Needed later to build a packet.">
          <textarea className={`${inputClass} min-h-32 py-2 font-mono text-sm`}
                    value={form.jd_text ?? ""}
                    onChange={(e) => set("jd_text", e.target.value)} />
        </Field>

        <div>
          <Button type="submit" variant="primary" loading={create.isPending}>
            Log it
          </Button>
        </div>
      </form>
    </div>
  );

  function UrlHint() {
    if (!check.data || check.data.status === "empty") return null;
    if (check.data.status === "unparseable") {
      return <span className="text-warn">{check.data.message}</span>;
    }
    if (check.data.status === "duplicate") {
      return (
        <span className="text-bad">
          Already tracked as application #{check.data.application?.id} (
          {check.data.application?.state}).
        </span>
      );
    }
    return (
      <span className="text-good">
        New. {check.data.ats_type ?? "direct"}
        {check.data.ats_slug ? ` · ${check.data.ats_slug}` : ""}
      </span>
    );
  }
}
