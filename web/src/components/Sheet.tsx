import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";

/** Radix, because focus trapping, scroll locking, Escape and aria-modal are
 *  genuinely hard to get right and this is the one place they are needed. */
export function Sheet({
  open, onOpenChange, title, side = "bottom", children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  side?: "bottom" | "full";
  children: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45 backdrop-blur-[2px]" />
        <Dialog.Content
          className={
            side === "bottom"
              ? "fixed inset-x-0 bottom-0 z-50 max-h-[85dvh] overflow-y-auto rounded-t-2xl border-t border-line bg-panel p-4 pb-[calc(1rem+env(safe-area-inset-bottom))] sm:inset-x-auto sm:right-4 sm:bottom-4 sm:w-96 sm:rounded-2xl sm:border"
              : "fixed inset-0 z-50 flex flex-col overflow-hidden bg-panel p-4 pb-[calc(1rem+env(safe-area-inset-bottom))] sm:inset-6 sm:rounded-2xl sm:border sm:border-line"
          }
        >
          <div className="mb-3 flex items-center justify-between gap-4">
            <Dialog.Title className="text-lg font-semibold tracking-tight">{title}</Dialog.Title>
            <Dialog.Close
              className="tap -mr-2 inline-flex items-center justify-center rounded-lg text-dim hover:text-ink"
              aria-label="Close"
            >
              <X className="size-5" />
            </Dialog.Close>
          </div>
          <div className={side === "full" ? "min-h-0 flex-1 overflow-y-auto" : ""}>{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
