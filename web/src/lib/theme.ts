export type Theme = "system" | "light" | "dark";

export function readTheme(): Theme {
  const stored = localStorage.getItem("theme");
  return stored === "light" || stored === "dark" ? stored : "system";
}

/** Kept in step with the blocking script in index.html, which sets the same
 *  attribute before first paint so a pinned dark theme never flashes white. */
export function applyTheme(theme: Theme) {
  if (theme === "system") {
    localStorage.removeItem("theme");
    delete document.documentElement.dataset.theme;
  } else {
    localStorage.setItem("theme", theme);
    document.documentElement.dataset.theme = theme;
  }
}
