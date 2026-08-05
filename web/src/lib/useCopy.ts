import { useCallback, useRef, useState } from "react";

/** Copy to clipboard, with the execCommand fallback kept.
 *
 *  navigator.clipboard needs a secure context. Over Tailscale Serve there is a
 *  real certificate so it works; over a bare http://100.x address it does not,
 *  and the fallback is ten lines. */
export function useCopy(): [boolean, (text: string) => void] {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  const copy = useCallback((text: string) => {
    const done = () => {
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1400);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done, () => fallback(text, done));
    } else {
      fallback(text, done);
    }
  }, []);

  return [copied, copy];
}

function fallback(text: string, done: () => void) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
    done();
  } finally {
    document.body.removeChild(area);
  }
}
