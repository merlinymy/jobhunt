import clsx from "clsx";
import { Check, Copy } from "lucide-react";
import { useCopy } from "../lib/useCopy";

/** The old version mutated textContent, which a screen reader never announces.
 *  aria-live carries the confirmation instead. */
export function CopyButton({
  value, children, className, block,
}: {
  value: string | null | undefined;
  children?: React.ReactNode;
  className?: string;
  block?: boolean;
}) {
  const [copied, copy] = useCopy();
  if (!value) return <span className="text-dim">—</span>;
  return (
    <button
      type="button"
      onClick={() => copy(value)}
      title="Copy"
      className={clsx(
        "group inline-flex items-start gap-2 rounded-lg border border-transparent px-2 py-1.5 text-left",
        "hover:border-line hover:bg-panel-2 focus-visible:border-line",
        block && "w-full",
        className,
      )}
    >
      <span className="min-w-0 flex-1 break-words">{children ?? value}</span>
      <span aria-hidden className="mt-0.5 shrink-0 text-dim group-hover:text-accent">
        {copied ? <Check className="size-4 text-good" /> : <Copy className="size-4" />}
      </span>
      <span className="sr-only" role="status" aria-live="polite">
        {copied ? "Copied" : ""}
      </span>
    </button>
  );
}
