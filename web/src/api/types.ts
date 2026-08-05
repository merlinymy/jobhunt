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
  answers: Answer[];
  unknowns: number;
  error: string | null;
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

export interface Spend {
  total: number;
  by_task: { task: string; calls: number; cost: number }[];
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
export type RunPipeline = "ingest_score" | "ingest" | "score";

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
  task: "ingest" | "score";
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

export interface Runs {
  active: Run | null;
  last: { ingest: Run | null; score: Run | null };
  phase_labels: Record<string, string>;
  waiting_to_score: number;
  waiting_to_review: number;
}
