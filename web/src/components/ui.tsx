import clsx from "clsx";
import type { ReactNode, ButtonHTMLAttributes } from "react";
import { Loader2 } from "lucide-react";

/* Hand-rolled rather than shadcn. Each of these is a dozen lines against the
 * token layer, and a stock component library is exactly the templated-default
 * look this rewrite exists to get away from. Radix is used where the behaviour
 * is genuinely hard — see Sheet. */

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "outline" | "ghost" | "danger";
  size?: "md" | "sm";
  loading?: boolean;
};

export function Button({
  variant = "outline", size = "md", loading, className, children, disabled, ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={clsx(
        "inline-flex items-center justify-center gap-2 rounded-lg font-medium",
        "transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
        size === "md" ? "tap px-4 text-[15px]" : "min-h-9 px-3 text-sm",
        variant === "primary" && "bg-accent text-bg hover:opacity-90",
        variant === "outline" && "border border-line bg-panel hover:bg-panel-2",
        variant === "ghost" && "hover:bg-panel-2 text-dim hover:text-ink",
        variant === "danger" && "border border-line text-bad hover:bg-panel-2",
        className,
      )}
    >
      {loading && <Loader2 className="size-4 animate-spin" aria-hidden />}
      {children}
    </button>
  );
}

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={clsx("card p-4 sm:p-5", className)}>{children}</div>;
}

export function Pill({ tone, children }: { tone: string; children: ReactNode }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium",
        "border border-current/25 bg-current/10 whitespace-nowrap",
        tone,
      )}
    >
      {children}
    </span>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-8 text-dim" role="status">
      <Loader2 className="size-4 animate-spin" aria-hidden />
      <span>{label}…</span>
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="card p-10 text-center">
      <p className="font-medium">{title}</p>
      {hint && <p className="mt-1 text-sm text-dim">{hint}</p>}
    </div>
  );
}

/** HTMX gave "the page renders anyway" for free. Without this, a failed query is
 *  a spinner that never resolves. */
export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="card border-bad/40 p-6" role="alert">
      <p className="font-medium text-bad">Could not load this.</p>
      <p className="mt-1 text-sm text-dim">{message}</p>
      {retry && (
        <Button className="mt-4" size="sm" onClick={retry}>
          Try again
        </Button>
      )}
    </div>
  );
}

export function Field({
  label, hint, children,
}: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="grid gap-1.5">
      <span className="text-sm font-medium">{label}</span>
      {children}
      {hint && <span className="text-xs text-dim">{hint}</span>}
    </label>
  );
}

/* 16px minimum, or iOS zooms the viewport when the field takes focus. */
export const inputClass =
  "tap w-full rounded-lg border border-line bg-panel px-3 text-base " +
  "placeholder:text-dim focus:border-accent";
