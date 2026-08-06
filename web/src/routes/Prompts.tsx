import clsx from "clsx";
import { FileText, History, RotateCcw, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useActivatePrompt, useRevertPrompt, useSavePrompt } from "../api/mutations";
import { usePrompt, usePrompts } from "../api/queries";
import { Button, Card, ErrorState, Spinner, inputClass } from "../components/ui";

/* Editing what the models are told, from here rather than a second window.
 *
 * The file in config/prompts stays the default and stays git-tracked; this
 * writes an override into `prompts` and Revert lifts it again. Everything is
 * keyed by the same sha `llm_calls.system_sha` records, so the history below
 * can say how many calls a wording actually produced — which is the only way to
 * tell an improvement from a mood. */

export default function Prompts() {
  const [params, setParams] = useSearchParams();
  const list = usePrompts();
  const task = params.get("task") ?? list.data?.prompts[0]?.task ?? "";
  const detail = usePrompt(task || null);

  const save = useSavePrompt(task);
  const revert = useRevertPrompt(task);
  const activate = useActivatePrompt(task);

  const [draft, setDraft] = useState<string | null>(null);
  const [note, setNote] = useState("");

  // The draft resets when the server's copy changes identity — switching task,
  // saving, reverting, activating an old revision. Keyed on the sha rather than
  // the body so an unrelated refetch cannot silently discard what is typed.
  const sha = detail.data?.sha ?? "";
  useEffect(() => {
    setDraft(null);
    setNote("");
  }, [sha, task]);

  if (list.isPending) return <Spinner label="Loading prompts" />;
  if (list.error) return <ErrorState error={list.error} retry={() => void list.refetch()} />;

  const body = draft ?? detail.data?.body ?? "";
  const dirty = draft !== null && draft.trim() !== (detail.data?.body ?? "").trim();

  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">Prompts</h1>
      <p className="mb-4 text-sm text-dim">
        What each task is told. Saved here, a revision takes effect on the next call —
        nothing to restart. The file in the repo stays the default, and Revert goes back to
        it.
      </p>

      <div className="mb-4 flex flex-wrap gap-1.5">
        {list.data?.prompts.map((entry) => (
          <button
            key={entry.task}
            onClick={() => setParams({ task: entry.task })}
            className={clsx(
              "rounded-lg border px-3 py-1.5 text-sm",
              entry.task === task
                ? "border-accent bg-panel-2 font-medium text-ink"
                : "border-line text-dim hover:text-ink",
            )}
          >
            {entry.task}
            {entry.source === "database" && (
              <span className="ml-1.5 text-xs text-accent" title="edited here">
                ●
              </span>
            )}
          </button>
        ))}
      </div>

      {detail.isPending && <Spinner label="Loading" />}
      {detail.error && <ErrorState error={detail.error} />}

      {detail.data && (
        <>
          <Card className="mb-3">
            <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm text-dim">
              <span className="font-medium text-ink">{detail.data.task}</span>
              <span>{detail.data.model}</span>
              <span className="tabular">{detail.data.sha}</span>
              <span className="ml-auto inline-flex items-center gap-1">
                <FileText className="size-3.5" aria-hidden />
                {detail.data.source === "database"
                  ? `overriding ${detail.data.file_path}`
                  : detail.data.file_path}
              </span>
            </div>

            <textarea
              className={clsx(inputClass, "min-h-[26rem] resize-y py-2 font-mono text-[13px] leading-relaxed")}
              value={body}
              spellCheck={false}
              onChange={(e) => setDraft(e.target.value)}
              aria-label={`${detail.data.task} system prompt`}
            />

            <div className="mt-3 flex flex-wrap items-center gap-2">
              <input
                className={clsx(inputClass, "flex-1 sm:max-w-xs")}
                placeholder="What changed, and why (optional)"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                aria-label="Revision note"
              />
              <Button
                variant="primary"
                disabled={!dirty}
                loading={save.isPending}
                onClick={() => save.mutate({ body, note })}
              >
                <Save className="size-4" aria-hidden /> Save
              </Button>
              {detail.data.source === "database" && (
                <Button
                  loading={revert.isPending}
                  onClick={() => revert.mutate()}
                  title="Drop the override and use the file in the repo"
                >
                  <RotateCcw className="size-4" aria-hidden /> Revert to file
                </Button>
              )}
              {dirty && <span className="text-sm text-warn">unsaved</span>}
            </div>

            {save.error && (
              <p className="mt-2 text-sm text-bad">{(save.error as Error).message}</p>
            )}
          </Card>

          {detail.data.history.length > 0 && (
            <Card>
              <p className="mb-2 flex items-center gap-2 text-sm font-medium">
                <History className="size-4 text-dim" aria-hidden /> Revisions
              </p>
              {/* Calls and cost per revision, because "is this better" is not a
                  question you can answer from the wording alone. */}
              <div className="grid gap-1.5">
                {detail.data.history.map((rev) => (
                  <div
                    key={rev.sha}
                    className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 rounded-lg border border-line px-3 py-2 text-sm"
                  >
                    <span className="tabular text-dim">{rev.sha}</span>
                    {rev.active && <span className="text-accent">live</span>}
                    <span className="text-dim">{rev.chars} chars</span>
                    <span className="tabular text-dim">
                      {rev.calls} call{rev.calls === 1 ? "" : "s"}
                      {rev.cost > 0 && ` · $${rev.cost.toFixed(2)}`}
                    </span>
                    {rev.note && <span className="text-dim">— {rev.note}</span>}
                    {!rev.active && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="ml-auto"
                        onClick={() => activate.mutate(rev.sha)}
                      >
                        Use this
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
