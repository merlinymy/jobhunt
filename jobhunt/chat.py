"""Gap-filling: work through `unknown_questions` and close them. `make chat`.

A form asked something the answer bank could not answer, `answers.flag_unknowns`
recorded it, and `seen_count` went up every time another form asked it again.
This is the other end of that: the queue, most-asked first, and one decision per
question.

Where an answer *goes* is the whole design, and it is not one place.

  A fact that facts.yaml has a home for  ->  named, never written here.
      `docs/profile/` is the source of truth and the DB is derived from it, so a
      DB row typed here would be a second source that the next `make
      load-profile` silently disagrees with. .claude/rules/data-layer.md is
      unconditional: build a loader, never a CRUD editor for this data. So this
      prints the exact dotted key to fill and gets out of the way. Fill it, run
      `make load-profile`, run this again — the question closes itself.

  A per-company fact  ->  pointed at the packet page.
      Salary is the category. The number depends on the job's market, so there is
      no global answer to type, and the one place that knows which employer is
      asking is the packet.

  Anything else  ->  typed here, stored fact-tier, `source = 'user'`.
      A question no catalogue anticipated has no file representation, which puts
      it in the same category as the generated per-company answers CLAUDE.md
      already exempts: it accumulates in the DB only, because there is nowhere
      else for it to live.

Nothing here calls a model. `answers.tier = 'fact'` is returned verbatim and is
never LLM-generated — that is the invariant this command exists to serve, not one
it gets to trade against. The only writer is `answers.put`.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys

from . import answers
from .db import connect


def resolve_answered(conn: sqlite3.Connection) -> int:
    """Close every unknown whose answer has since arrived. Returns how many.

    Run before anything is asked, on every invocation. The usual way a gap gets
    filled is that the dotted key printed last time was added to facts.yaml and
    `make load-profile` imported it — at which point nobody wants to be asked
    about it again, and nothing else was going to notice.
    """
    closed = 0
    for row in answers.open_unknowns(conn):
        question = answers.catalogued(row["question_text"])
        if question is None:
            continue
        # Global scope only. A per-company answer closes one employer's form, not
        # the question, so it must not retire the row for every other employer.
        existing = answers.lookup(conn, question.key, None)
        if existing is not None and str(existing["answer"]).strip():
            answers.mark_resolved(conn, int(row["id"]), int(existing["id"]))
            closed += 1
    return closed


def describe(row: sqlite3.Row) -> str:
    """The header for one question: how often, and where it was first hit."""
    times = "once" if row["seen_count"] == 1 else f"{row['seen_count']} times"
    where = ""
    if row["company"] and row["title"]:
        where = f" · first on {row['title']} at {row['company']}"
    return f"asked {times}{where}"


def _prompt(text: str) -> str:
    """One line from the terminal, or "" when there is no terminal to read."""
    try:
        return input(text).strip()
    except EOFError:
        return ""


def walk(conn: sqlite3.Connection, *, interactive: bool = True) -> dict[str, int]:
    """The queue, one question at a time. Returns counts by what happened."""
    counts = {"closed": 0, "in_file": 0, "per_company": 0, "narrative": 0,
              "answered": 0, "skipped": 0}
    counts["closed"] = resolve_answered(conn)

    queue = answers.open_unknowns(conn)
    if not queue:
        print(
            "nothing to fill in — every question a form has asked has an answer."
            + (f"\n{counts['closed']} closed by an answer that arrived since the last run."
               if counts["closed"] else "")
        )
        return counts

    print(f"{len(queue)} unanswered question(s), most-asked first.\n")
    for index, row in enumerate(queue, start=1):
        question = answers.catalogued(row["question_text"])
        print(f"[{index}/{len(queue)}] {row['question_text']}")
        print(f"        {describe(row)}")

        if question is not None and question.tier != answers.FACT:
            # First, ahead of the routing below, because it is the invariant and
            # they are conveniences. Unreachable through `flag_unknowns`, which
            # records fact-tier only — but if a narrative question ever did reach
            # here with a `facts_path` set by mistake, checking `facts_path` first
            # would route it as a fact and the mistake would be invisible. What
            # that costs is a narrative answer stored fact-tier, and fact-tier is
            # the one that gets returned verbatim forever without a model in the
            # path. Cheap to order correctly; expensive to get wrong.
            print(
                "        Narrative, so it is drafted per company rather than decided\n"
                "        once. Generate it on the packet for the application asking."
            )
            counts["narrative"] += 1
            print()
            continue

        if question is not None and question.facts_path:
            # The file is the interface. Say exactly which line to add.
            print(
                f"        This one belongs in docs/profile/facts.yaml under\n"
                f"          {question.facts_path}\n"
                f"        Add it there and run `make load-profile` — the DB is derived\n"
                f"        from that file, so typing it here would be a second answer\n"
                f"        that the next load disagrees with."
            )
            if question.note:
                print(f"        {question.note}")
            counts["in_file"] += 1
            print()
            continue

        if question is not None and question.per_company:
            print(
                "        Per-employer, so there is no global answer to type. Set it on\n"
                "        the packet for the application that asked — it is reused verbatim\n"
                "        for every later application to that company."
            )
            counts["per_company"] += 1
            print()
            continue

        # No catalogue entry, so no file and no company: the DB is the only home.
        if not interactive:
            print("        (--list, so nothing was asked)")
            counts["skipped"] += 1
            print()
            continue

        print("        Type an answer, or press enter to skip. `q` quits.")
        typed = _prompt("      > ")
        if typed.lower() == "q":
            print("\nstopped.")
            break
        if not typed:
            counts["skipped"] += 1
            print()
            continue

        key = question.key if question else _key_for(row["question_text"])
        answer_id = answers.put(
            conn,
            key,
            row["question_text"],
            typed,
            tier=answers.FACT,
            # Typed by hand, so `user`. Nothing here is generated — that is what
            # makes it fact-tier and what lets it be returned verbatim.
            source="user",
        )
        answers.mark_resolved(conn, int(row["id"]), answer_id)
        counts["answered"] += 1
        print(f"        stored as {key}\n")

    return counts


def _key_for(question_text: str) -> str:
    """A stable key for a question with no catalogue entry.

    Same shape and same reasoning as `formfill._key_for`: derived from the
    wording, because there is no catalogue to assign from, so two forms asking
    the same thing in the same words land on one row.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", question_text.lower()).strip("_")
    return f"asked_{slug[:60]}" if slug else "asked_question"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Work through the questions forms asked that had no answer."
    )
    parser.add_argument(
        "--list", action="store_true",
        help="report the queue and where each answer belongs, asking nothing",
    )
    args = parser.parse_args(argv)

    conn = connect()
    try:
        counts = walk(conn, interactive=not args.list)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        return 130
    finally:
        conn.close()

    reported = {name: n for name, n in counts.items() if n}
    if reported:
        print(" · ".join(f"{n} {name.replace('_', ' ')}" for name, n in reported.items()))
    if counts["in_file"]:
        print(
            f"\n{counts['in_file']} of these belong in docs/profile/facts.yaml. Fill them,\n"
            f"then: make load-profile && make chat"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
