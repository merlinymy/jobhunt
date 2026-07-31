# Intake Questionnaire

Populates `profile_facts`, `bullets`, `answers`, `contacts`, and the scoring config. Work
through it in chunks — Section C is the long one and is worth doing properly.

**Two rules that make this worth the effort:**

1. **Capture 3–5× more than fits on a resume.** The tailor step can only *select* from what's
   here. A one-page master profile produces one-page-shaped tailoring. This is the whole reason
   for the depth in Section C.
2. **Numbers or it doesn't count.** A bullet without a metric is nearly unusable for tailoring.
   For every accomplishment, force the question: *how many, how much, how long, before vs after.*

**Where answers go** — write them in files, not in this document. This is the prompt; those
are the data.

| Sections | File |
| --- | --- |
| A, H | `docs/profile/facts.yaml` |
| B | `docs/profile/scoring.yaml` |
| C, D | `docs/profile/experience.yaml` |
| E, F | `docs/profile/stories.md` (prose on purpose) |
| G | `docs/profile/contacts.csv` (LinkedIn export + hand additions) |

Those files are the source of truth. `make load-profile` imports them into SQLite and is
re-runnable, so fill them incrementally.

---

## A. Identity and decided-once answers
*→ `docs/profile/facts.yaml`. Three blocks: `identity` feeds the resume header, `decided_once`
stores answers I'd otherwise improvise differently each time, and `comp` holds my walk-away
number plus what research needs to price the role.
Since I fill forms by hand, the test for a field is **would I have to decide or look this
up?** If I'd type it from memory, it's not here.*

1. Legal name exactly as on your ID and tax documents. Preferred name if different.
2. Email and phone you want on applications. City and state. *(Resume header.)*
3. LinkedIn, GitHub, portfolio, personal site — whichever you'd actually put on a resume.
4. Are you legally authorized to work in the US? *(Exact yes/no. Knockout question.)*
5. Will you now or in the future require sponsorship? *(Also knockout. Answer precisely.)*
   If on a visa: type, expiry, timeline pressure.
6. **Comp:** → the `comp:` block. Only one number is decided in advance — `walk_away`, below
   which you don't take the job anywhere. That's a fact about your life, not the market, so
   it's answerable now. Everything city-specific is decided per application, because pay moves
   with market, arrangement, and level and you can't price a city you haven't seen a job in.
   Also here: the *phrasing* you use when a form demands one figure, and which titles and level
   research should price. Comp never filters a posting.
7. Earliest start date and notice period.
8. Onsite tolerance — remote only, hybrid (how many days), or onsite acceptable?
9. Relocation — open to it? Which metros? Would you need assistance?
10. Security clearance, if you hold one or expect to be asked.
*(References are not here — they're people, so they live in `contacts.csv` with the rest of the
network. See section G.)*

**Deliberately not collected:** street address, EEO responses, travel percentage, "how did you
hear about us," license numbers, driver's license. Your password manager's identity autofill
handles those on the actual form better than this system would, and no one cross-references
them between applications. EEO in particular is voluntary, aggregate-only, and separated from
your application — there's nothing to keep consistent.

## B. What "suits me" means
*→ scoring config for `score.py` pass 1. This is deterministic filtering, so be strict — the
prefilter should kill ~90% of ingested jobs.*

19. Title **keywords** — a few words that appear in titles you'd accept (`engineer`,
    `developer`). Substring match, so keep it short and wide. Don't enumerate exact titles:
    real ones vary too much and a missed variant is a lost shot, silently.
20. Title **blocklist** — words that mean automatic no (e.g. "Intern," "Staffing," "Manager"
    if you're IC-track, "Contract" if you want FTE). This is the list that should be precise.
21. Your seniority band, and how much over/under-leveling you'll tolerate.
22. *(No comp question here. Comp never filters — see `facts.yaml` `comp:` for my walk-away
    number, which exists to answer with, not to reject postings.)*
23. Location **preference order**, best first, ending in a catch-all — it ranks the digest
    rather than rejecting anything. Plus timezone constraints, which only apply to remote roles.
24. Company size and stage preference. Any hard limits (no pre-seed, no 10,000+)?
25. Industries you want. Industries you'd refuse.
26. **Company blocklist** — competitors, past employers, anywhere you won't work.
27. Technologies or domains that must appear in the JD. Ones that are dealbreakers.
28. Hard exclusions: agency/staffing postings, clearance-required, heavy on-call, unpaid,
    commission-only, anything else.
29. Years-of-experience range in the JD you'll still apply to when you're outside it.

---

## C. Experience — the bullet mine
*→ `experiences` + `bullets`. Repeat this entire block for every role, including ones you'd
normally cut. Aim for 8–12 bullets per significant role; the resume will use 3–5.*

For each position:

30. Exact title(s), including internal title changes and when they happened.
31. Employer, start and end month/year, location, employment type (FTE, contract, part-time).
32. What did the company do, and how big was it *at the time you were there*? (Context makes
    your scale claims legible — "grew to 50k users" reads differently at a 5-person startup.)
33. Your scope: team size, whether you had reports, budget owned, systems owned, users or
    customers served, revenue touched.
34. **Accomplishments, 8–12, each with a number.** For each one:
    - What was the state before? What was it after?
    - How many users, requests, dollars, hours, people, percent?
    - How long did it take? What did it take before?
    - Was this yours alone or shared? Be honest — this matters in interviews.
35. What were you *known for* on that team? What did people come to you for?
36. Technologies used, split by depth: built from scratch / owned and extended / maintained /
    touched occasionally. Don't flatten these into one skills list.
37. Promotions, awards, notable performance reviews, anything formally recognized.
38. Something that went badly and what you did about it. *(Feeds Section E.)*
39. Why did you leave? Your honest reason and your sayable reason — write both.

---

## D. Everything else that's evidence
*→ `projects`, `education`, `bullets`*

40. Side projects and open source: what it does, your role, users/stars/downloads, live URL.
41. Education: school, degree, field, dates, GPA if strong and recent, relevant coursework,
    honors. Note anything incomplete.
42. Certifications and licenses, with dates and expiry.
43. Publications, talks, workshops, podcasts, meaningful blog posts.
44. Volunteer or community work relevant to your target roles.
45. Languages and fluency level.
46. Patents, competitions, hackathons, rankings.

---

## E. Story bank
*→ new `stories` table. Tier: narrative. Answers the essay questions on forms, and becomes the
interview coach's corpus. Write these as situation → action → result, with the numbers.*

47. A conflict with a coworker or manager, and how it actually resolved.
48. A time you failed, missed a deadline, or shipped something broken.
49. An ambiguous problem you had to scope yourself.
50. A time you influenced a decision without authority.
51. The hardest technical problem you've solved. What made it hard?
52. A time data or someone's argument changed your mind.
53. Something you're proud of that nobody noticed.
54. Feedback that stung, and what you changed.
55. A time you pushed back on a bad idea — and one where you should have but didn't.
56. Something you taught, mentored, or unblocked someone on.

---

## F. Positioning and motivation
*→ `answers`, tier narrative, `company_id IS NULL` as the global default. Per-company
"why us" answers get generated from this raw material and cached.*

57. Why are you looking right now? Honest version and sayable version.
58. What do you want more of in the next role? Less of?
59. Rank what you're optimizing for: comp, scope, learning, stability, mission, team, autonomy,
    WLB. The ranking matters more than the list.
60. Your two-sentence pitch. What you do, who for, why you're good at it.
61. The three things you want every recruiter to walk away knowing.
62. Non-negotiables. What would make you turn down an otherwise good offer?
63. Describe a company you'd be genuinely excited to join — mission, product, culture,
    engineering values. *(This is the raw material the "why this company" generator draws on.
    Vague answers here produce generic cover letters.)*
64. Your five-year direction, stated plainly.
65. What would make you a bad fit somewhere? Where do you not thrive?

---

## G. Referral network
*→ `contacts`. Queried at digest time so approved jobs surface a "ping this person first"
prompt. This is the single highest-leverage section here.*

66. Former coworkers and managers you're on good terms with — and where they work now.
67. Anyone who has offered to help, or would say yes if you asked.
68. Alumni networks, bootcamp cohorts, communities with a job channel you can post in.
69. People who already know your work well enough to vouch without a warm-up conversation.
70. **Do not contact:** anyone the system should never surface.
71. Export your LinkedIn connections to CSV once (Settings → Data Privacy → Get a copy of your
    data), and import company + name. No platform automation, just the file.
72. **References live here too**, as contacts with `relationship: reference`. Put their email in
    `handle` with `channel: email`. Note that whether they've *agreed* has no structured column
    yet — record it in `notes` until a real form demands more.

---

## Running this

- **A and B first.** They're short and they unblock scoring plus most form fields.
- **C is the real work.** One role per sitting. Have your old resumes, performance reviews,
  and commit history open — you have forgotten most of your own metrics.
- **E and F reward being written out longhand**, then compressed by the system. Don't
  pre-summarize; you'll strip out the specifics that make the output non-generic.
- **Decide once, then freeze:** comp phrasing and the sponsorship answer. Inconsistency across
  applications to the same company is a real cost.
- **Expect this list to be incomplete.** Phase 0 — applying to 3–4 jobs by hand and logging
  the questions that surprised you — is what turns this into the actual taxonomy. Anything a real form
  asks that isn't here goes into `unknown_questions` and gets added.
