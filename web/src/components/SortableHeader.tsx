import clsx from "clsx";
import { ChevronDown, ChevronUp } from "lucide-react";
import type { SortHeader } from "../api/types";

/** Server-decided sort state. `next_direction` encodes the rule that counts and
 *  dates open highest-first while names open A–Z, so the client does not hold a
 *  second copy of it. */
export function SortableHeader({
  header, onSort,
}: { header: SortHeader; onSort: (column: string, direction: "asc" | "desc") => void }) {
  return (
    <th
      scope="col"
      aria-sort={header.active ? (header.direction === "asc" ? "ascending" : "descending") : "none"}
      className={clsx(
        "border-b border-line px-3 py-2 text-sm font-medium whitespace-nowrap",
        header.numeric ? "text-right" : "text-left",
      )}
    >
      <button
        type="button"
        onClick={() => onSort(header.column, header.next_direction)}
        className={clsx(
          "inline-flex items-center gap-1 rounded hover:text-accent",
          header.active ? "text-ink" : "text-dim",
        )}
      >
        {header.label}
        {header.active &&
          (header.direction === "asc" ? (
            <ChevronUp className="size-3.5" aria-hidden />
          ) : (
            <ChevronDown className="size-3.5" aria-hidden />
          ))}
      </button>
    </th>
  );
}
