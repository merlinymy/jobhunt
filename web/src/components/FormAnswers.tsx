import { Check, ClipboardList, Loader2, ShieldCheck, AlertTriangle } from "lucide-react";
import { useState } from "react";

import { ApiError } from "../api/client";
import { useChooseFormAnswer, useDraftFormAnswers } from "../api/mutations";
import type { FormAnswer } from "../api/types";
import { CopyButton } from "./CopyButton";
import { Button, Card, Pill } from "./ui";

/** Answers for the free-text boxes on this application's form.
 *
 *  Paste the questions, get five options each, copy the one you want. Five
 *  rather than one because picking beats editing: a single draft gets rewritten
 *  by hand every time, and five different arguments usually contain one worth
 *  sending.
 *
 *  Questions the profile has already settled — work authorization, notice
 *  period, salary — are not drafted at all. They come back verbatim from
 *  `facts.yaml` with one option and a note saying where the words came from,
 *  because those have exactly one correct answer and a rephrasing of one is a
 *  chance to get it wrong. Where the profile has no answer recorded, it says so
 *  rather than offering an invented one.
 */
export function FormAnswers({ id, answers }: { id: number; answers: FormAnswer[] }) {
  const [pasted, setPasted] = useState("");
  const draft = useDraftFormAnswers(id);
  const choose = useChooseFormAnswer(id);

  const error = draft.error
    ? draft.error instanceof ApiError
      ? draft.error.message
      : "Could not reach the server. Is it still running?"
    : null;

  return (
    <Card className="mb-4">
      <p className="flex items-center gap-2 font-medium">
        <ClipboardList className="size-4 shrink-0" aria-hidden />
        Application questions
      </p>
      <p className="mt-1 text-sm text-dim">
        Paste the form's questions — one per line, or separated by blank lines.
        Anything your profile has already settled comes back verbatim instead of
        drafted.
      </p>

      <textarea
        className="mt-3 min-h-[6rem] w-full rounded-lg border border-line bg-panel-2 p-3 text-[15px] outline-none focus:border-accent"
        placeholder={"Why do you want to work here?\nAre you authorized to work in the US?\nDescribe a technical problem you solved."}
        value={pasted}
        disabled={draft.isPending}
        onChange={(e) => setPasted(e.target.value)}
      />
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <Button
          variant="primary"
          loading={draft.isPending}
          disabled={draft.isPending || !pasted.trim()}
          onClick={() => draft.mutate(pasted)}
        >
          {draft.isPending ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden />
              Drafting
            </>
          ) : answers.length > 0 ? (
            "Redraft"
          ) : (
            "Draft answers"
          )}
        </Button>
        {answers.length > 0 && (
          <span className="text-sm text-dim">
            Redrafting replaces the set below.
          </span>
        )}
        {error && <span className="text-sm text-bad">{error}</span>}
      </div>

      {answers.length > 0 && (
        <ol className="mt-5 space-y-5">
          {answers.map((a, i) => (
            <li key={i} className="border-t border-line pt-4">
              <div className="flex flex-wrap items-center gap-2">
                <Pill tone={a.source === "fact" ? "text-good" : "text-dim"}>
                  {a.source === "fact" ? "from your profile" : "drafted"}
                </Pill>
                <span className="font-medium">{a.question}</span>
              </div>

              {a.note && (
                <p
                  className={`mt-1 flex items-center gap-1.5 text-sm ${
                    a.options.length > 0 ? "text-dim" : "text-warn"
                  }`}
                >
                  {a.options.length > 0 ? (
                    <ShieldCheck className="size-4 shrink-0" aria-hidden />
                  ) : (
                    <AlertTriangle className="size-4 shrink-0" aria-hidden />
                  )}
                  {a.note}
                </p>
              )}

              <ul className="mt-2 space-y-2">
                {a.options.map((option, j) => {
                  const picked = a.chosen === j;
                  const bad = a.unsourced[String(j)];
                  return (
                    <li
                      key={j}
                      className={`rounded-lg border p-3 ${
                        picked ? "border-accent bg-accent/5" : "border-line bg-panel-2"
                      }`}
                    >
                      <p className="select-text text-[15px] leading-relaxed">{option}</p>
                      <div className="mt-2 flex flex-wrap items-center gap-3">
                        <CopyButton value={option}>Copy</CopyButton>
                        {a.source === "draft" &&
                          (picked ? (
                            <span className="flex items-center gap-1.5 text-sm text-accent">
                              <Check className="size-4" aria-hidden />
                              Chosen — saved for this company
                            </span>
                          ) : (
                            <button
                              className="text-sm text-dim hover:text-accent"
                              disabled={choose.isPending}
                              onClick={() => choose.mutate({ index: i, option: j })}
                            >
                              Use this one
                            </button>
                          ))}
                        {bad && bad.length > 0 && (
                          /* Same check the gap answers get. This goes in a box a
                             person reads and can follow up on, so an invented
                             figure is worse here than on paper. */
                          <span className="flex items-center gap-1.5 text-sm text-bad">
                            <AlertTriangle className="size-4 shrink-0" aria-hidden />
                            {bad.join(", ")} not in your corpus
                          </span>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
