"""Adversarial fixtures for the tailoring validator.

One of the three tests this project permits (CLAUDE.md, "No test suite"). It
exists because `.claude/rules/tailoring.md` requires it: the no-fabrication
guarantee rests entirely on `tailor.validate`, and nothing else checks it.

Every case in FABRICATIONS must be REJECTED. Every case in LEGITIMATE must be
ACCEPTED — a validator that rejects everything is not a validator.

Run it directly; no pytest required:

    .venv/bin/python tests/test_tailoring_validator.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobhunt import config, tailor  # noqa: E402
from jobhunt.db import connect  # noqa: E402

# --------------------------------------------------------------------------
# A corpus small enough to reason about, with the two properties that matter:
# a bullet carrying a metric, and a bullet marked shared credit.
# --------------------------------------------------------------------------

EXPERIENCE = {
    "company": "Northwind Logistics",
    "title": "Senior Backend Engineer",
    "start_month": "2021-03",
    "end_month": "2024-06",
}
EDUCATION = {
    "school": "State University",
    "degree": "MS",
    "field": "Computer Science",
}
# A second parent, so "claimed a tool from a different job" is expressible.
PROJECT = {"name": "Fleet Dashboard", "tech_built": ["Kubernetes", "Grafana"]}
EXPERIENCE_TECH = ["Postgres", "Python"]

BULLETS = [
    # id 1: has a metric, sole credit
    {
        "text": "Cut p99 checkout latency from 840ms to 210ms by replacing the "
        "synchronous pricing call with a cached read path.",
        "metric": "840ms to 210ms",
        "solo": 1,
    },
    # id 2: shared credit, no metric
    {
        "text": "Migrated the order service off a shared Postgres instance as part "
        "of a four-engineer platform team.",
        "metric": None,
        "solo": 0,
    },
    # id 3: percentage metric, sole credit
    {
        "text": "Reduced infrastructure spend 18% by rightsizing over-provisioned "
        "worker pools.",
        "metric": "18%",
        "solo": 1,
    },
    # id 4: source that already claims breadth and spells a number out, so the
    # scope and number-word checks can be shown allowing what the corpus says.
    {
        "text": "Backfilled all eleven reporting tables after the schema change.",
        "metric": "11 tables",
        "solo": 1,
    },
]
# id 5, under the project rather than the experience.
PROJECT_BULLET = {
    "text": "Built the rollout pipeline that ships the dashboard to each cluster.",
    "metric": None,
    "solo": 1,
}


def build_corpus(db_path: Path):
    from jobhunt import migrate

    config.DB_PATH = db_path
    migrate.main()
    conn = connect()
    conn.execute(
        "INSERT INTO experiences (id, company, title, start_month, end_month,"
        " sort_order, tech_built) VALUES (1, ?, ?, ?, ?, 0, ?)",
        (
            EXPERIENCE["company"],
            EXPERIENCE["title"],
            EXPERIENCE["start_month"],
            EXPERIENCE["end_month"],
            json.dumps(EXPERIENCE_TECH),
        ),
    )
    conn.execute(
        "INSERT INTO projects (id, name, sort_order, tech_built) VALUES (1, ?, 0, ?)",
        (PROJECT["name"], json.dumps(PROJECT["tech_built"])),
    )
    conn.execute(
        "INSERT INTO education (id, school, degree, field, complete) VALUES (1, ?, ?, ?, 1)",
        (EDUCATION["school"], EDUCATION["degree"], EDUCATION["field"]),
    )
    for index, bullet in enumerate(BULLETS, start=1):
        conn.execute(
            "INSERT INTO bullets (id, experience_id, text, metric, solo, sort_order)"
            " VALUES (?, 1, ?, ?, ?, ?)",
            (index, bullet["text"], bullet["metric"], bullet["solo"], index - 1),
        )
    conn.execute(
        "INSERT INTO bullets (id, project_id, text, metric, solo, sort_order)"
        " VALUES (5, 1, ?, ?, ?, 0)",
        (PROJECT_BULLET["text"], PROJECT_BULLET["metric"], PROJECT_BULLET["solo"]),
    )
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# Must be rejected. The five named in .claude/rules/tailoring.md come first.
# --------------------------------------------------------------------------

FABRICATIONS: list[tuple[str, list[dict]]] = [
    (
        "invented employer",
        [{"id": 1, "text": "At Stripe, cut p99 checkout latency from 840ms to 210ms."}],
    ),
    (
        "shifted date",
        [{"id": 1, "text": "Since 2019, cut p99 checkout latency from 840ms to 210ms."}],
    ),
    (
        "inflated metric",
        [{"id": 3, "text": "Reduced infrastructure spend 45% by rightsizing worker pools."}],
    ),
    (
        "fabricated degree",
        [{"id": 1, "text": "Applied PhD-level queueing theory to cut latency 840ms to 210ms."}],
    ),
    (
        "shared credit rewritten as solo",
        [{"id": 2, "text": "I single-handedly migrated the order service off shared Postgres."}],
    ),
    # Beyond the required five: the failures a real model actually produces.
    (
        "bullet id that does not exist",
        [{"id": 99, "text": "Led the platform rewrite."}],
    ),
    (
        "same source used twice",
        [
            {"id": 1, "text": "Cut p99 checkout latency from 840ms to 210ms."},
            {"id": 1, "text": "Improved checkout latency substantially."},
        ],
    ),
    (
        "metric borrowed from a different bullet",
        [{"id": 2, "text": "Migrated the order service, reducing spend 18%."}],
    ),
    (
        "number invented outright",
        [{"id": 2, "text": "Migrated 47 services off a shared Postgres instance."}],
    ),
    (
        "shared credit claimed with 'my'",
        [{"id": 2, "text": "Drove my migration of the order service off shared Postgres."}],
    ),
    ("empty text", [{"id": 1, "text": "   "}]),
    ("no bullets at all", []),
    ("missing the id field", [{"text": "Cut latency."}]),
    # Bypasses found auditing Phase 2. Each of these was ACCEPTED before the fix,
    # and each is a one-word edit away from a case that was already rejected —
    # which is exactly why the originals were not enough on their own.
    (
        "invented employer as the first word (was: only caught after 'At ')",
        [{"id": 1, "text": "Stripe checkout latency cut from 840ms to 210ms."}],
    ),
    (
        "invented tool after a semicolon (clause-initial bypass)",
        [{"id": 3, "text": "Rightsized worker pools; Datadog dashboards proved the 18% saving."}],
    ),
    (
        "invented product opening the second sentence",
        [{"id": 2, "text": "Migrated the order service off shared Postgres. Kubernetes handled the cutover."}],
    ),
    (
        "metric changed downward to a small number (was: whitelisted)",
        [{"id": 3, "text": "Reduced infrastructure spend 5% by rightsizing worker pools."}],
    ),
    (
        "invented team size (was: whitelisted)",
        [{"id": 1, "text": "Led 8 engineers to cut p99 checkout latency from 840ms to 210ms."}],
    ),
    (
        "invented years of experience (was: whitelisted)",
        [{"id": 1, "text": "Applied 10 years of latency work, cutting p99 from 840ms to 210ms."}],
    ),
    (
        "invented scope count (was: whitelisted)",
        [{"id": 1, "text": "Cut p99 checkout latency from 840ms to 210ms across 100 services."}],
    ),
    # The four classes the pronoun/proper-noun/digit checks could not see.
    # Shared credit claimed with an ownership verb and no banned word:
    (
        "shared credit taken over by 'Led'",
        [{"id": 2, "text": "Led the migration of the order service off shared Postgres."}],
    ),
    (
        "shared credit taken over by 'Owned'",
        [{"id": 2, "text": "Owned and executed the order service migration off shared Postgres."}],
    ),
    (
        "shared credit taken over by 'Spearheaded'",
        [{"id": 2, "text": "Spearheaded the order service migration off shared Postgres."}],
    ),
    # Scope widened with ordinary words — no proper noun, no number:
    (
        "scope widened to the whole company",
        [{"id": 2, "text": "Migrated the entire company platform off shared Postgres "
                           "with a four-engineer team."}],
    ),
    (
        "scope widened to 'every'",
        [{"id": 3, "text": "Reduced infrastructure spend 18% by rightsizing every worker pool."}],
    ),
    # Numbers spelled out:
    (
        "invented count written as a word",
        [{"id": 2, "text": "Migrated forty services off a shared Postgres instance "
                           "with a four-engineer team."}],
    ),
    (
        "metric restated as a word at a different value",
        [{"id": 3, "text": "Reduced infrastructure spend by thirty percent by rightsizing "
                           "worker pools."}],
    ),
    # Identifiers carrying a number. The lookbehind that stops `p99` reading as a
    # bare `9` also hid the whole token, so these were all accepted: a changed
    # percentile, a changed version, a changed molecule.
    (
        "percentile shifted (p99 -> p95)",
        [{"id": 1, "text": "Cut p95 checkout latency from 840ms to 210ms."}],
    ),
    (
        "percentile weakened (p99 -> p50)",
        [{"id": 1, "text": "Cut p50 checkout latency from 840ms to 210ms."}],
    ),
    (
        "count smuggled in as an identifier",
        [{"id": 1, "text": "Cut p99 latency from 840ms to 210ms across svc12 deployments."}],
    ),
    # Homoglyph: Cyrillic Ѕ, which leaves `tripe` for the ASCII word regex.
    (
        "invented employer spelled with a Cyrillic S",
        [{"id": 1, "text": "Cut p99 checkout latency from 840ms to 210ms at Ѕtripe."}],
    ),
    # A technology the corpus knows, but from a different parent:
    (
        "tool borrowed from another parent, lowercase",
        [{"id": 1, "text": "Cut p99 checkout latency from 840ms to 210ms using kubernetes."}],
    ),
    (
        "tool borrowed from another parent, capitalised",
        [{"id": 1, "text": "Cut p99 checkout latency from 840ms to 210ms on Grafana dashboards."}],
    ),
]

# --------------------------------------------------------------------------
# Must be accepted. Reordering, rewording, tightening, dropping — all permitted.
# --------------------------------------------------------------------------

LEGITIMATE: list[tuple[str, list[dict]]] = [
    (
        "verbatim",
        [{"id": 1, "text": BULLETS[0]["text"]}],
    ),
    (
        "reworded, same numbers",
        [{"id": 1, "text": "Drove p99 checkout latency down from 840ms to 210ms with a cached read path."}],
    ),
    (
        "tightened, numbers dropped entirely",
        [{"id": 3, "text": "Rightsized over-provisioned worker pools to cut infrastructure spend."}],
    ),
    (
        "reordered subset",
        [
            {"id": 3, "text": "Reduced infrastructure spend 18% by rightsizing worker pools."},
            {"id": 1, "text": "Cut p99 checkout latency from 840ms to 210ms."},
        ],
    ),
    (
        "shared credit kept plural",
        [{"id": 2, "text": "Migrated the order service off shared Postgres with a four-engineer team."}],
    ),
    (
        "shared credit, team-voice verb",
        [{"id": 2, "text": "Contributed to the order service migration off shared Postgres."}],
    ),
    # Guards on the Phase 2 fixes. Checking clause-initial words and dropping the
    # small-number whitelist both fail toward rejection, so these pin down what
    # must keep passing.
    (
        "clause-initial verb reworded from the source ('replacing' -> 'Replaced')",
        [{"id": 1, "text": "Replaced the synchronous pricing call with a cached read path, "
                           "cutting p99 checkout latency from 840ms to 210ms."}],
    ),
    (
        "second sentence opening with a reworded verb",
        [{"id": 1, "text": "Cut p99 checkout latency from 840ms to 210ms. Replaced the "
                           "synchronous pricing call with a cached read path."}],
    ),
    (
        "opens with a verb the source never uses",
        [{"id": 1, "text": "Shipped a cached read path that cut p99 checkout latency "
                           "from 840ms to 210ms."}],
    ),
    (
        "identifier digits reworded into prose (p99 -> 99th percentile)",
        [{"id": 1, "text": "Cut checkout latency at the 99th percentile from 840ms to 210ms."}],
    ),
    (
        "shared credit, clause-initial team verb",
        [{"id": 2, "text": "Migrated the order service off shared Postgres. Worked alongside "
                           "a four-engineer platform team."}],
    ),
    # Guards on the four checks above. Each fails toward rejection, so what must
    # keep passing has to be pinned down as tightly as what must not.
    (
        # Opens on a verb that is neither team-voice nor the source's, so it
        # passes only because the other people are still named.
        "shared credit, ownership verb but the team is still named",
        [{"id": 2, "text": "Drove the order service migration off shared Postgres "
                           "alongside a four-engineer platform team."}],
    ),
    (
        "shared credit, source's own verb kept",
        [{"id": 2, "text": "Migrated the order service off a shared Postgres instance."}],
    ),
    (
        "scope word the source itself claims",
        [{"id": 4, "text": "Backfilled all eleven reporting tables after the schema change."}],
    ),
    (
        "spelled number the source itself uses",
        [{"id": 2, "text": "Migrated the order service off shared Postgres with a team of four."}],
    ),
    (
        "spelled number matching the source's digits",
        [{"id": 4, "text": "Backfilled eleven reporting tables after the schema change."}],
    ),
    (
        "digits matching the source's spelled number",
        [{"id": 4, "text": "Backfilled 11 reporting tables after the schema change."}],
    ),
    (
        "technology belonging to this bullet's own parent",
        [{"id": 5, "text": "Built the Kubernetes rollout pipeline that ships the dashboard "
                           "to each cluster."}],
    ),
    (
        "ordinary lowercase word that is not a corpus name",
        [{"id": 1, "text": "Cut p99 checkout latency from 840ms to 210ms with an in-memory "
                           "cache in front of the pricing call."}],
    ),
    # Guards on the identifier and homoglyph checks. Both fail toward rejection,
    # so what must keep passing is pinned as tightly as what must not.
    (
        "the source's own identifier, kept",
        [{"id": 1, "text": "Held p99 checkout latency at 210ms, down from 840ms."}],
    ),
    (
        "unit glued to a sourced number is not an identifier",
        [{"id": 1, "text": "Cut checkout latency from 840ms to 210ms."}],
    ),
    (
        "identifier from the parent's tech list, not this bullet's sentence",
        [{"id": 5, "text": "Built the rollout pipeline on Kubernetes, shipping the dashboard "
                           "to each cluster."}],
    ),
]


def run() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        conn = build_corpus(Path(tmp) / "fixtures.db")

        for name, emitted in FABRICATIONS:
            try:
                tailor.validate(conn, emitted)
            except tailor.TailorError:
                print(f"  ok      rejected: {name}")
            else:
                failures.append(f"NOT REJECTED: {name}")
                print(f"  FAIL    accepted a fabrication: {name}")

        for name, emitted in LEGITIMATE:
            try:
                tailor.validate(conn, emitted)
            except tailor.TailorError as exc:
                failures.append(f"WRONGLY REJECTED: {name} ({exc})")
                print(f"  FAIL    rejected legitimate tailoring: {name}\n          {exc}")
            else:
                print(f"  ok      accepted: {name}")

        conn.close()

    total = len(FABRICATIONS) + len(LEGITIMATE)
    print(f"\n{total - len(failures)}/{total} cases behaved correctly")
    for line in failures:
        print(f"  {line}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
