import clsx from "clsx";
import {
  BarChart3, ClipboardList, FileText, Inbox, Monitor, Moon, PenLine, Plus, Sun, Wand2,
} from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet, ScrollRestoration } from "react-router-dom";
import { Sheet } from "./components/Sheet";
import { applyTheme, readTheme, type Theme } from "./lib/theme";

/* Six destinations, not ten. The old nav was a flat no-wrap flex row of ten
 * interactive elements, which is what broke at 390px — the fix is fewer places
 * to go, not smaller text. */
type NavItem = { to: string; label: string; icon: typeof Sun; end?: boolean };

const PRIMARY: NavItem[] = [
  { to: "/review", label: "Review", icon: ClipboardList },
  { to: "/", label: "Pipeline", icon: Inbox, end: true },
  { to: "/fill", label: "Fill", icon: FileText },
  { to: "/stats", label: "Stats", icon: BarChart3 },
];

const SECONDARY: NavItem[] = [
  { to: "/applications/new", label: "Log application", icon: Plus },
  { to: "/tailor", label: "Tailor", icon: Wand2 },
];

const THEMES: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: "system", label: "Auto", icon: Monitor },
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
];

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(readTheme);
  return (
    <div className="inline-flex rounded-lg border border-line p-0.5" role="group" aria-label="Colour theme">
      {THEMES.map(({ value, label, icon: Icon }) => (
        <button
          key={value}
          type="button"
          aria-pressed={theme === value}
          title={label}
          onClick={() => {
            applyTheme(value);
            setTheme(value);
          }}
          className={clsx(
            "inline-flex size-8 items-center justify-center rounded-md",
            theme === value ? "bg-panel-2 text-ink" : "text-dim hover:text-ink",
          )}
        >
          <Icon className="size-4" aria-hidden />
          <span className="sr-only">{label}</span>
        </button>
      ))}
    </div>
  );
}

export default function App() {
  const [more, setMore] = useState(false);

  return (
    <div className="min-h-dvh">
      {/* Desktop: a top bar. */}
      <header className="sticky top-0 z-30 hidden border-b border-line bg-bg/85 backdrop-blur md:block">
        <nav className="mx-auto flex max-w-7xl items-center gap-1 px-6 py-2.5">
          <span className="mr-4 font-semibold tracking-tight">jobhunt</span>
          {[...PRIMARY, ...SECONDARY].map(({ to, label, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                clsx(
                  "rounded-lg px-3 py-1.5 text-sm",
                  isActive ? "bg-panel-2 font-medium text-ink" : "text-dim hover:text-ink",
                )
              }
            >
              {label}
            </NavLink>
          ))}
          <div className="ml-auto">
            <ThemeToggle />
          </div>
        </nav>
      </header>

      {/* Mobile: a title row, and the real navigation at the bottom where the
          thumb is. This is a one-handed triage app before it is anything else. */}
      <header className="sticky top-0 z-30 flex items-center justify-between border-b border-line bg-bg/85 px-4 py-2.5 backdrop-blur md:hidden">
        <span className="font-semibold tracking-tight">jobhunt</span>
        <ThemeToggle />
      </header>

      <main className="px-4 pb-24 pt-4 sm:px-6 md:pb-16">
        <Outlet />
      </main>

      <nav
        className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t border-line bg-panel/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden"
        aria-label="Main"
      >
        {PRIMARY.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              clsx(
                "flex h-14 flex-col items-center justify-center gap-0.5 text-[11px]",
                isActive ? "text-accent" : "text-dim",
              )
            }
          >
            <Icon className="size-5" aria-hidden />
            {label}
          </NavLink>
        ))}
        <button
          type="button"
          onClick={() => setMore(true)}
          className="flex h-14 flex-col items-center justify-center gap-0.5 text-[11px] text-dim"
        >
          <PenLine className="size-5" aria-hidden />
          More
        </button>
      </nav>

      <Sheet open={more} onOpenChange={setMore} title="More">
        <div className="grid gap-2">
          {SECONDARY.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              onClick={() => setMore(false)}
              className="tap flex items-center gap-3 rounded-lg border border-line px-3 hover:bg-panel-2"
            >
              <Icon className="size-4 text-dim" aria-hidden />
              {label}
            </NavLink>
          ))}
        </div>
      </Sheet>

      <ScrollRestoration />
    </div>
  );
}
