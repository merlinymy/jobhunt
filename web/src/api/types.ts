/** Response shapes, written by hand.
 *
 * The server returns plain dicts rather than Pydantic models — a response model
 * per endpoint would be a second schema to keep in step with hand-written SQL.
 * `tsc --noEmit` in `make check-web` catches this half of the contract.
 */

export interface Meta {
  pipeline_order: string[];
  dead_ends: string[];
  terminal: string[];
  transitions: Record<string, string[]>;
  sortable: Record<string, string>;
  descending_first: string[];
  conversion_sortable: Record<string, string>;
  state_table_sortable: Record<string, string>;
  stats_tables: Record<string, string>;
  unknown_ats: string;
  filter_fields: string[];
  review_limit: number;
  today: string;
}

export interface Application {
  id: number;
  state: string;
  company_name: string;
  title: string;
  apply_url: string;
  location: string | null;
  remote: string | null;
  source: string | null;
  comp_min: number | null;
  comp_max: number | null;
  applied_at: string | null;
  first_response_at: string | null;
  would_apply_anyway: number | null;
  referral_contact_id: number | null;
  company_id: number;
  score: number | null;
  score_reasoning: string | null;
  ats_type: string | null;
  ats_slug: string | null;
  next_states: string[];
  is_terminal: boolean;
  has_resume: number | boolean;
  has_answers: number | boolean;
}

export interface SortHeader {
  column: string;
  label: string;
  active: boolean;
  direction: "asc" | "desc" | null;
  next_direction: "asc" | "desc";
  query: string;
  numeric?: boolean;
}

export interface Facets {
  state: string[];
  ats: string[];
  source: string[];
}

export interface ApplicationList {
  applications: Application[];
  total: number;
  headers: SortHeader[];
  filters: Record<string, string>;
  facets: Facets;
  sort: string;
  direction: "asc" | "desc";
}

export interface Honesty {
  answered: number;
  yes: number;
  ratio: number | null;
}

export interface QueueHealth {
  approved_backlog: number;
  oldest_days: number | null;
  applied_this_month: number;
}

export interface Pipeline extends ApplicationList {
  counts: Record<string, number>;
  health: QueueHealth;
  honesty: Honesty;
  funnel: Record<string, string>;
  active_filters: number;
  clear_query: string;
}

export interface ReviewCard {
  application_id: number;
  title: string;
  company: string;
  where: string;
  score: number;
  reason: string;
  apply_url: string;
  comp: string | null;
  referral: string | null;
  excerpt: string;
  also_in: number;
}

export interface ReviewBatch {
  batch: ReviewCard[];
  waiting: number;
  limit: number;
}

export interface Decision {
  application_id: number;
  title: string;
  outcome: "approved" | "skipped";
  siblings_closed: number;
  /** Approving starts the packet it was approved for. "started" if this click
   *  did, "queued" if a build was already running and will pick the row up,
   *  otherwise the reason. Absent on a skip. */
  packet?: string;
}

export interface EventRow {
  id: number;
  application_id: number;
  kind: string;
  from_state: string | null;
  to_state: string | null;
  detail: string | null;
  created_at: string;
}

export interface Contact {
  id: number;
  name: string;
  company_id: number | null;
  company_name: string | null;
  relationship: string | null;
  handle: string | null;
  do_not_contact: number;
}

export interface Detail {
  app: Application;
  events: EventRow[];
  referrals: Contact[];
}

export interface DiffRow {
  bullet_id: number | null;
  before: string;
  after: string;
  changed: boolean;
}

export interface Answer {
  key: string;
  text: string;
  tier: string;
  answer: string | null;
  source: string | null;
  missing: boolean;
  optional: boolean;
}

export interface DuplicateListing {
  id: number;
  state: string;
  location: string | null;
}

/** One thing a check objected to in the stored resume.
 *
 *  These no longer block the build — the packet renders and these come back
 *  alongside it, to be acted on or dismissed by the person who reads the resume
 *  before sending it. `source` is what the corpus actually says, which is the
 *  only question worth asking about a flag. */
export interface Finding {
  kind:
    | 'number'
    | 'identifier'
    | 'homoglyph'
    | 'unknown'
    | 'duplicate'
    | 'review'
    | 'unchecked';
  where: string;
  message: string;
  bullet_id: number | null;
  source: string;
  /** The line was dropped from the resume, not merely flagged on it. */
  blocking: boolean;
}

export interface Packet {
  app: Application;
  where: string;
  duplicates: number;
  duplicate_rows: DuplicateListing[];
  apply_url: string;
  referral: string | null;
  has_jd: boolean;
  diff: DiffRow[];
  pdf: { bullets: number; reworded: number } | null;
  /** `null` never built · `[]` built and nothing objected · non-empty flagged.
   *  The middle case is a positive statement and must not render as the first. */
  findings: Finding[] | null;
  answers: Answer[];
  unknowns: number;
  /** The live packet run, or null. The `packet` lock is global to the task, so
   *  this may be the `job_approved` batch rather than this row — still the
   *  honest answer to why the button is refusing, and the phase says which. */
  run: Run | null;
  /** The last packet run of any kind, so a failure from a build that finished
   *  while the page was closed is still visible on the next open. */
  last_run: Run | null;
  phase_labels: Record<string, string>;
  resume_text: ResumeText | null;
  /** What the posting asks for that the corpus cannot support. `gaps_analysed`
   *  distinguishes "checked, nothing missing" from "never checked", which are
   *  very different statements to make to someone about to apply. */
  gaps: Gap[];
  gaps_analysed: boolean;
  messages: ChatMessage[];
  error: string | null;
}

/** The stored resume as text. The PDF is what gets submitted; this is what you
 *  can read on a phone and quote from into the chat box. */
export interface ResumeText {
  summary: string;
  /** `n` is the line number the model is shown, so "line 3" means the same
   *  thing on the page and in the prompt. Server-side for that reason — a
   *  second numbering in TypeScript is a second numbering to drift. */
  lines: { n: number; id: number | null; text: string }[];
  /** Summary plus bulleted lines, no source annotations. For pasting into a
   *  cover letter or an ATS box. */
  plain: string;
  /** Exactly what the model is shown, annotations included. */
  prompt_view: string;
}

/** One thing the posting wants that the corpus cannot support. */
export interface Gap {
  wanted: string;
  severity: "required" | "plus";
  have: string;
  /** Source rows evidencing the adjacent experience. These were handed to the
   *  tailor before it chose, so this evidence is already on the resume. */
  bullet_ids: number[];
  /** The answer to give verbatim when a form asks. */
  say: string;
  /** Figures in `say` that no corpus row contains. Normally empty. */
  unsourced: string[];
}

/** A proposed revision: the whole resume as it would read, not a patch. */
export interface ChatProposal {
  summary: string;
  bullets: { id: number; text: string }[];
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  /** null when the turn answered a question and changed nothing. */
  proposal: ChatProposal | null;
  /** Set once the proposal was rendered onto the row. */
  applied_at: string | null;
  created_at?: string;
}

export interface DateForms {
  iso: string;
  slash: string;
  month: string;
  month_name: string;
  year: string;
}

export interface FillEntry {
  company?: string;
  name?: string;
  title?: string;
  role?: string | null;
  url?: string | null;
  location?: string | null;
  employment_type?: string | null;
  start: DateForms | null;
  end: DateForms | null;
  bullets: string[];
  description: string;
}

export interface FillEducation {
  school: string;
  degree: string | null;
  field: string | null;
  start: DateForms | null;
  end: DateForms | null;
}

export interface Fill {
  identity: { label: string; value: string }[];
  experiences: FillEntry[];
  projects: FillEntry[];
  education: FillEducation[];
}

export interface Bucket {
  label: string;
  applied: number;
  responded: number;
  interviews: number;
  offers: number;
  rejected: number;
  pending: number;
  response_rate: number | null;
  interview_rate: number | null;
  offer_rate: number | null;
}

export interface StatsTable {
  prefix: string;
  rows: Bucket[];
  sort: string;
  direction: "asc" | "desc";
  headers: SortHeader[];
}

/** `queries.llm_spend` over a rolling window. These names are the server's —
 *  the previous `{ total, by_task }` described a shape it has never returned,
 *  and since the types here are hand-written nothing caught it until the page
 *  crashed on `undefined.toFixed`. */
export interface Spend {
  days: number;
  calls: number;
  cost: number;
  failed: number;
  truncated: number;
  /** null when nothing cacheable was sent — an unused cache and an absent one
   *  are different situations, so this is not folded to 0. */
  hit_rate: number | null;
}

export interface Stats {
  overall: Bucket;
  honesty: Honesty;
  health: QueueHealth;
  spend: Spend;
  tables: Record<string, StatsTable>;
}

export interface UrlCheck {
  status: "empty" | "unparseable" | "duplicate" | "new";
  message?: string;
  normalized?: string;
  ats_type?: string | null;
  ats_slug?: string | null;
  job?: { id: number; title: string; company_id: number };
  application?: { id: number; state: string } | null;
}

export interface TailorResult {
  reasoning: string;
  diff: DiffRow[];
  pdf: string;
  kept: number;
  reworded: number;
}

/** Discovery and scoring runs. The server holds the lock and the progress; this
 *  is a read of `runs`, not a state machine the client gets to have opinions
 *  about. A `RunPipeline` is what a button asks for; a `task` is one step of it.
 *  Not `Pipeline` — that is already the funnel this fills. */
export type RunPipeline = "ingest_score" | "ingest" | "score" | "packet";

export interface RunProgress {
  phase: string;
  message: string;
  done: number;
  /** null when the denominator is genuinely unknown — show a spinner, not a
   *  bar, rather than inventing one. */
  total: number | null;
  /** Already labelled and ordered by the worker, so a new counter needs no
   *  change here. */
  counts: Record<string, number>;
}

export interface Run {
  id: number;
  task: "ingest" | "score" | "packet";
  chain: string[];
  step: number;
  steps: number;
  state: "running" | "done" | "failed" | "interrupted";
  trigger: "dashboard" | "cli" | "launchd";
  started_at: string;
  finished_at: string | null;
  /** Since it finished, or since it started while it still is. Server-side, so
   *  a laptop with a skewed clock cannot report a run from the future. */
  age_seconds: number | null;
  progress: RunProgress | null;
  error: string | null;
}

export interface PromptRevision {
  sha: string;
  created_at: string;
  active: boolean;
  note: string;
  chars: number;
  /** Calls logged against this sha. The reason revisions are keyed by the same
   *  hash `llm_calls.system_sha` uses: "is this wording better" is not
   *  answerable from the wording. */
  calls: number;
  cost: number;
}

export interface PromptDetail {
  task: string;
  body: string;
  sha: string;
  /** Which of the two is in force. "file" means no override is saved. */
  source: "file" | "database";
  file_body: string;
  file_path: string;
  differs_from_file: boolean;
  model: string;
  history: PromptRevision[];
}

export interface PromptSummary {
  task: string;
  model: string;
  source: "file" | "database";
  sha: string;
  chars: number;
  updated_at: string | null;
  missing: boolean;
}

export interface JobDescription {
  title: string;
  company: string;
  apply_url: string;
  /** Empty, never null — plenty of board rows have no description at all. */
  jd_text: string;
}

export interface Runs {
  active: Run | null;
  last: { ingest: Run | null; score: Run | null; packet: Run | null };
  phase_labels: Record<string, string>;
  waiting_to_score: number;
  waiting_to_review: number;
}
