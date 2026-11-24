// Known gaps of the volume gate: views that exceed the budget today (measured: board/reports, decisions/KOR-35.md).
// A listed test is marked `test.fail(...)` with `KOR-35 gap: <reason>`, so the gate stays green while the gap stays
// visible in every run; a gap that is closed makes its test pass and therefore fail as "expected to fail" until its
// entry is removed here. The budgets themselves are never relaxed. Stage (b) of KOR-35 removes these entries.
import { test, type TestInfo } from '@playwright/test';

type Project = 'volume' | 'volume-mobile';

interface Gap {
  readonly projects: readonly Project[];
  readonly reason: string;
}

const BOTH: readonly Project[] = ['volume', 'volume-mobile'];

/** By test title (the first id a test measures). */
export const GAPS: Readonly<Record<string, Gap>> = {};

/** Marks the running test as a known gap of its project, if listed. */
export function knownGap(info: TestInfo, key: string): void {
  const gap = GAPS[key];
  if (gap && gap.projects.includes(info.project.name as Project)) test.fail(true, `KOR-35 gap: ${gap.reason}`);
}

export { BOTH };
