// The test runs of a session (ADR-078, 13-work-vm.md section 12): the operation `test.run` runs a command in a fresh
// VM and writes the event `test.run` when the run starts (`running`) and when it ends.
//
// - `parseTestRun`: the decrypted body checked against `contracts/events/objects.json` (`body_test_run`: every field
//   required, no other field); anything else is null and the chat shows a neutral row instead of content;
// - `parseTestRunResult`: the operation's result (`result_test_run` of `contracts/ops/objects.json`) for the card;
// - `selectTestRuns`: the latest valid body per `run_id` of one session, newest start first (the session panel; the
//   timeline keeps the latest item per run through `latestRuns` of `workvm.ts`);
// - `summaryCounts` / `runFailed`: the parts of the summary text ("Tests: 3 failed, 12 passed") and whether the run
//   is red.
import {
  type Check,
  count,
  type LogRef,
  logRef,
  matches,
  newestFirst,
  nullable,
  oneOf,
  pattern,
  sortedEntries,
  text,
  time,
  uuid,
  integer,
} from './shape';
import type { ScopeEvents } from '../store/slices/events';
import type { StoreState } from '../store/store';

export const TEST_RUN_KIND = 'test.run';
export const TEST_RUN_TYPE = 'test.run';

export const TEST_KINDS = ['lint', 'format', 'unit', 'integration', 'security', 'custom'] as const;
export type TestKind = (typeof TEST_KINDS)[number];
export const TEST_STATES = ['running', 'passed', 'failed', 'error', 'timed_out', 'cancelled'] as const;
export type TestState = (typeof TEST_STATES)[number];
export const TEST_REASONS = [
  'credit_exhausted',
  'restart',
  'start_failed',
  'transfer_failed',
  'vm_lost',
  'cancelled',
] as const;
export type TestReason = (typeof TEST_REASONS)[number];

/** The counts of a summary line the runner recognised; each null when the line does not name it. */
export interface TestSummary {
  readonly passed: number | null;
  readonly failed: number | null;
  readonly skipped: number | null;
  readonly errors: number | null;
}

/** `result_test_run`: the result of the operation. */
export interface TestRunResult {
  readonly run_id: string;
  readonly state: TestState;
  readonly exit_code: number | null;
  readonly duration_ms: number;
  readonly ready_ms: number;
  readonly summary: TestSummary | null;
  readonly log: LogRef | null;
  readonly tail: string;
  readonly reason: TestReason | null;
}

/** `body_test_run`: the result fields plus the run's parameters and times. */
export interface TestRun extends TestRunResult {
  readonly operation_id: string;
  readonly kind: TestKind;
  readonly command: string;
  readonly cwd: string;
  readonly size: 'standard' | 'large' | 'xlarge';
  readonly started_at: string;
  readonly ended_at: string | null;
}

const SUMMARY_SHAPE: Readonly<Record<keyof TestSummary, Check>> = {
  passed: nullable(count),
  failed: nullable(count),
  skipped: nullable(count),
  errors: nullable(count),
};

const RESULT_SHAPE: Readonly<Record<keyof TestRunResult, Check>> = {
  run_id: pattern(/^tr-[0-9a-f]{12}$/),
  state: oneOf(...TEST_STATES),
  exit_code: nullable(integer(0, 255)),
  duration_ms: count,
  ready_ms: count,
  summary: (value) => value === null || matches(value, SUMMARY_SHAPE),
  log: logRef,
  tail: text(8192),
  reason: nullable(oneOf(...TEST_REASONS)),
};

const RUN_SHAPE: Readonly<Record<keyof TestRun, Check>> = {
  ...RESULT_SHAPE,
  operation_id: uuid,
  kind: oneOf(...TEST_KINDS),
  command: text(4096),
  cwd: text(4096),
  size: oneOf('standard', 'large', 'xlarge'),
  started_at: time,
  ended_at: nullable(time),
};

/** A `test.run` body, or null when it does not follow the contract. */
export const parseTestRun = (body: unknown): TestRun | null =>
  matches(body, RUN_SHAPE) ? (body as unknown as TestRun) : null;

/** A `test.run` operation result, or null when it does not follow the contract. */
export const parseTestRunResult = (result: unknown): TestRunResult | null =>
  matches(result, RESULT_SHAPE) ? (result as unknown as TestRunResult) : null;

/** Whether the run ended red: anything but `running` and `passed`. */
export const runFailed = (run: Pick<TestRunResult, 'state'>): boolean =>
  run.state !== 'running' && run.state !== 'passed';

const SUMMARY_ORDER = ['failed', 'errors', 'passed', 'skipped'] as const;
export type SummaryPart = (typeof SUMMARY_ORDER)[number];

/** The non-zero counts of a summary, red ones first (`[["failed", 3], ["passed", 12]]`); empty without counts. */
export function summaryCounts(summary: TestSummary | null): [SummaryPart, number][] {
  if (summary === null) return [];
  return SUMMARY_ORDER.filter((part) => (summary[part] ?? 0) > 0).map((part) => [part, summary[part]!]);
}

function collectRuns(scope: ScopeEvents | undefined, task: string, session: string): readonly TestRun[] {
  const latest = new Map<string, TestRun>();
  for (const entry of sortedEntries(scope)) {
    if (entry.status !== 'verified' || entry.header!.kind !== TEST_RUN_KIND) continue;
    if (entry.header!.task !== task || entry.header!.session !== session) continue;
    const body = parseTestRun(entry.body);
    if (body === null) continue;
    latest.delete(body.run_id);
    latest.set(body.run_id, body);
  }
  return newestFirst([...latest.values()]);
}

/** A selector of the latest valid `test.run` body per run of `session`, newest start first (memoized on the scope). */
export function selectTestRuns(wg: string, task: string, session: string): (state: StoreState) => readonly TestRun[] {
  let last: { scope: unknown; runs: readonly TestRun[] } | null = null;
  return (state) => {
    const scope = state.events[wg];
    if (last !== null && last.scope === scope) return last.runs;
    last = { scope, runs: collectRuns(scope, task, session) };
    return last.runs;
  };
}
