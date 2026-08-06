import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

import type { ResumeText as ResumeTextData } from "../api/types";
import { CopyButton } from "./CopyButton";
import { Card } from "./ui";

/** The resume as readable, selectable text.
 *
 *  The PDF is the artifact that gets submitted, and it is the wrong thing for
 *  every other purpose: you cannot read it on a phone without pinching, you
 *  cannot select a line out of it to paste into an ATS box, and you certainly
 *  cannot quote from it into the chat below without retyping.
 *
 *  Lines are numbered, and the numbers are the server's — the same ones the
 *  model is shown. That is the whole point: "line 3 is weak" has to land on the
 *  same line at both ends of the conversation, and a second numbering computed
 *  in TypeScript is a second numbering to drift.
 */
export function ResumeText({ data }: { data: ResumeTextData | null }) {
  const [open, setOpen] = useState(true);
  if (!data) return null;

  return (
    <Card className="mb-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button
          className="flex items-center gap-1.5 font-medium hover:text-accent"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? (
            <ChevronDown className="size-4" aria-hidden />
          ) : (
            <ChevronRight className="size-4" aria-hidden />
          )}
          Resume text
          <span className="ml-1 text-sm font-normal text-dim">
            {data.lines.length} line{data.lines.length === 1 ? "" : "s"}
          </span>
        </button>
        <CopyButton value={data.plain}>Copy</CopyButton>
      </div>

      {open && (
        <div className="mt-3 space-y-4">
          {data.summary && (
            <div>
              <p className="mb-1 text-sm text-dim">Summary</p>
              {/* select-text and a real <p>, not a <pre>: this is prose meant to
                  be read and dragged over, and a monospace block reads as code. */}
              <p className="select-text text-[15px] leading-relaxed">{data.summary}</p>
            </div>
          )}
          <div>
            <p className="mb-1 text-sm text-dim">Lines</p>
            <ol className="space-y-2">
              {data.lines.map((line) => (
                <li key={line.n} className="flex gap-3">
                  {/* tabular-nums so the numbers form a column rather than
                      shifting the text left and right at 10. */}
                  <span className="w-5 shrink-0 text-right text-sm tabular-nums text-dim">
                    {line.n}
                  </span>
                  <span className="select-text text-[15px] leading-relaxed">
                    {line.text}
                  </span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      )}
    </Card>
  );
}
