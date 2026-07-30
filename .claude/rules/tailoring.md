---
paths:
  - "**/tailor*.py"
  - "**/prompts/**"
  - "**/resume/**"
---

# Resume tailoring rules

The no-fabrication guarantee rests entirely on this code. There is no test suite elsewhere in
the project; these rails are the substitute.

## What the tailor may and may not do

The prompt receives `bullets` rows including their IDs, metrics, and `solo` flag. It returns
selected bullet IDs plus reworded text for each.

**Permitted:** selecting a subset, reordering, rewording, adjusting emphasis, changing which
skills are foregrounded, tightening for length.

**Forbidden, without exception:** introducing an employer, job title, date, degree,
certification, or metric that does not appear in the source rows. Inflating a number.
Shifting a date. Converting shared credit (`solo = 0`) into individual credit. Merging two
bullets in a way that implies a scope neither had.

## Validator

Runs after every tailor call, before the PDF is rendered. It raises — it does not warn, and it
does not fall back to the untailored resume silently.

Checks, at minimum:

1. Every emitted bullet maps to a real `bullets.id` from the input set
2. Every number in the output appears in the source `text` or `metric` of its own bullet
3. Employer names, titles, and date ranges match `experiences` exactly
4. Degrees and schools match `education` exactly
5. No bullet from `solo = 0` is reworded into first-person-singular ownership

Adversarial fixtures live alongside the validator and must include: an invented employer, a
shifted end date, an inflated percentage, a fabricated degree, and a shared-credit bullet
rewritten as solo. All must be rejected.

## Diff surfacing

Every tailored resume gets a diff against master rendered in the dashboard before I use it.
Reworded bullets show old and new side by side. If a diff cannot be produced, treat that as a
failure and block the packet.

## Output format

Single column. Standard section headings. No tables, no text boxes, no multi-column layouts,
no contact info in headers or footers — Workday and Taleo parsers mishandle all of these.
Dates as `Jan 2023 – Mar 2025`. Real selectable text, never rasterized.
