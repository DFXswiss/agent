// Known gaps of the volume gate: views that exceed the budget today (measured: board/reports, decisions/KOR-35.md).
// A listed test is marked `test.fail(...)` with `KOR-35 gap: <reason>`, so the gate stays green while the gap stays
// visible in every run; a gap that is closed makes its test pass and therefore fail as "expected to fail" until its
// entry is removed here. The budgets themselves are never relaxed. Stage (b) of KOR-35 removes these entries.
import { test, type TestInfo } from '@playwright/test';

type Project = 'volume' | 'volume-mobile';

/** Both projects (for entries of `GAPS`). */
export const BOTH: readonly Project[] = ['volume', 'volume-mobile'];

interface Gap {
  readonly projects: readonly Project[];
  readonly reason: string;
}

/** By test title (the first id a test measures). */
export const GAPS: Readonly<Record<string, Gap>> = {};

/**
 * Covered ids the data set cannot contain today, with the reason: the seed reports them (`seed.json`, `unseedable`),
 * the global teardown accepts them as not measured only while they are listed here – and fails when the seed could
 * produce them after all (the entry must go) or reports one that is not listed. Counted as gaps in the report.
 */
export const UNSEEDABLE: Readonly<Record<string, string>> = {};

/** The known gap of `key` in the running test's project, if listed. */
export function gapOf(info: TestInfo, key: string): Gap | null {
  const gap = GAPS[key];
  return gap && gap.projects.includes(info.project.name as Project) ? gap : null;
}

/** Marks the running test as a known gap of its project, if listed. */
export function knownGap(info: TestInfo, key: string): void {
  const gap = gapOf(info, key);
  if (gap) test.fail(true, `KOR-35 gap: ${gap.reason}`);
}
