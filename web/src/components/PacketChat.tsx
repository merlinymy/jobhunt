import { Check, CornerDownLeft, Loader2, MessageSquare } from "lucide-react";
import { useState } from "react";

import { useApplyProposal, useSendChat } from "../api/mutations";
import type { ChatMessage } from "../api/types";
import { ApiError } from "../api/client";
import { Button, Card } from "./ui";

/** Openers worth one tap. The first is the one that produced this feature. */
const SUGGESTIONS = [
  "Drop any claim the checks flagged.",
  "Lead with the work closest to this posting.",
  "Why did you leave out the rest?",
];

/** Arguing with the resume, in the session that wrote it.
 *
 *  The model here is continuing the conversation that produced the draft — it
 *  has its own prompt, its own reply, and the reasoning it wrote at the time —
 *  so "why did you drop that bullet" is answered from the decision rather than
 *  reconstructed. That is a backend property; what it means here is that the
 *  thread is worth showing in full rather than treating as a scratch box.
 *
 *  A turn either answers or proposes. A proposal is the *whole* resume as it
 *  would then read, and nothing touches the stored PDF until Apply — so a
 *  revision that made things worse costs a click rather than the draft.
 */
export function PacketChat({
  id,
  messages,
  disabled,
}: {
  id: number;
  messages: ChatMessage[];
  disabled: boolean;
}) {
  const [draft, setDraft] = useState("");
  const send = useSendChat(id);
  const apply = useApplyProposal(id);

  function submit() {
    const said = draft.trim();
    if (!said || send.isPending) return;
    // Cleared optimistically: the turn is echoed back in the thread, and a box
    // that still holds what you just sent invites sending it twice.
    setDraft("");
    send.mutate(said, { onError: () => setDraft(said) });
  }

  const error = send.error
    ? send.error instanceof ApiError
      ? send.error.message
      : "Could not reach the server. Is it still running?"
    : null;

  return (
    <Card className="mb-4">
      <p className="flex items-center gap-2 font-medium">
        <MessageSquare className="size-4 shrink-0" aria-hidden />
        Talk about this resume
      </p>
      <p className="mt-1 text-sm text-dim">
        Continues the conversation that wrote it, so it can say why it chose what it
        chose. Nothing changes the PDF until you apply it.
      </p>

      {messages.length > 0 && (
        <ol className="mt-4 space-y-3">
          {messages.map((m) => (
            <li key={m.id}>
              {m.role === "user" ? (
                <div className="ml-auto max-w-[85%] rounded-lg rounded-br-sm bg-accent/10 px-3 py-2 text-[15px]">
                  {m.content}
                </div>
              ) : (
                <div className="max-w-[85%]">
                  <p className="whitespace-pre-wrap text-[15px] leading-relaxed">
                    {m.content}
                  </p>
                  {m.proposal && (
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      {m.applied_at ? (
                        <span className="flex items-center gap-1.5 text-sm text-good">
                          <Check className="size-4" aria-hidden />
                          Applied — {m.proposal.bullets.length} lines
                        </span>
                      ) : (
                        <>
                          <Button
                            variant="primary"
                            loading={apply.isPending}
                            disabled={apply.isPending || disabled}
                            onClick={() => apply.mutate(m.id)}
                          >
                            Apply these {m.proposal.bullets.length} lines
                          </Button>
                          <span className="text-sm text-dim">
                            re-renders the PDF and re-runs the checks
                          </span>
                        </>
                      )}
                    </div>
                  )}
                </div>
              )}
            </li>
          ))}
        </ol>
      )}

      {messages.length === 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              className="tap rounded-lg border border-line px-3 text-left text-sm text-dim hover:border-accent hover:text-accent"
              onClick={() => setDraft(s)}
            >
              {s}
            </button>
          ))}
        </div>
      )}

      <div className="mt-4">
        <textarea
          className="min-h-[4.5rem] w-full rounded-lg border border-line bg-panel-2 p-3 text-[15px] outline-none focus:border-accent"
          placeholder='e.g. "line 3 is weak — is there a better one in the corpus?"'
          value={draft}
          disabled={send.isPending}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends, Shift+Enter breaks the line. This is a chat box, and
            // the common case by far is one sentence.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <span className="flex items-center gap-1.5 text-sm text-dim">
            <CornerDownLeft className="size-3.5" aria-hidden />
            Enter to send · Shift+Enter for a new line
          </span>
          <Button
            variant="primary"
            loading={send.isPending}
            disabled={send.isPending || !draft.trim() || disabled}
            onClick={submit}
          >
            {send.isPending ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden />
                Thinking
              </>
            ) : (
              "Send"
            )}
          </Button>
        </div>
        {/* Inline rather than a toast: a rejected message is one you have to
            retype, and eight seconds is not long enough to act on that. */}
        {error && <p className="mt-2 text-sm text-bad">{error}</p>}
      </div>
    </Card>
  );
}
