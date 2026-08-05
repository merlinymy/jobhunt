import { useSyncExternalStore } from "react";

/** Pick a renderer rather than rendering both and hiding one with CSS.
 *  At the table's 500-row cap that would be a thousand DOM nodes for nothing. */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}

/** Tailwind's md. Above it, real tables; below, cards. */
export const useIsDesktop = () => useMediaQuery("(min-width: 768px)");
