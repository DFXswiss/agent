// The volume data set of the worker's lane (seeded once per lane by `seed` through the device double,
// `scripts/hub_test_volume.py`) and the follow-up calls of the volume specs.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { expect } from '@playwright/test';
import { directorOrigin, laneOf, WEB_DIR } from '../fixtures/env.ts';
import { laneError, seedFile } from './completeness.ts';

export interface VolumeData {
  readonly org: string;
  readonly wg: string;
  readonly founder: string;
  readonly device: string;
  readonly big_task: string;
  readonly big_session: string;
  readonly big_child: string;
  readonly big_operation: string;
  readonly small_task: string;
  readonly small_session: string;
  readonly deep_session: string;
  readonly whiteboard_entry: string;
  readonly members: number;
  /** Covered ids the data set could not contain, with the reason (reported by the teardown, not measured). */
  readonly unseedable: Readonly<Record<string, string>>;
  readonly counts: Readonly<Record<string, number>>;
  readonly timings: Readonly<Record<string, number>>;
  readonly seconds: number;
  /** Restored from the cache (`seconds` and `timings` are those of the seed that made the entry). */
  readonly cached?: boolean;
}

export async function director(method: 'GET' | 'POST', path: string, body?: unknown): Promise<unknown> {
  const response = await fetch(`${directorOrigin()}${path}`, {
    method,
    headers: { 'content-type': 'application/json' },
    ...(body !== undefined && { body: JSON.stringify(body) }),
  });
  const data = (await response.json()) as unknown;
  if (!response.ok) throw new Error(`director ${method} ${path}: ${JSON.stringify(data)}`);
  return data;
}

/**
 * Waits until the lane's device double answers: the global setup starts a volume lane in the background, because it
 * may wait for the other volume lane's seed (decisions/HOT-21.md); a lane that failed to start is reported at once.
 */
async function laneReady(lane: string): Promise<void> {
  const failure = laneError(WEB_DIR, lane);
  const deadline = Date.now() + 25 * 60_000;
  for (;;) {
    if (existsSync(failure)) throw new Error(`volume lane ${lane} did not start: ${readFileSync(failure, 'utf8')}`);
    const up = await fetch(`${directorOrigin()}/health`).then(
      (response) => response.ok,
      () => false,
    );
    if (up) return;
    if (Date.now() > deadline) throw new Error(`volume lane ${lane} did not answer within 25 minutes`);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

interface SeedState {
  readonly state: 'idle' | 'running' | 'done' | 'failed';
  readonly data?: VolumeData;
  readonly error?: string;
}

/**
 * Seeds the data set in the lane of the worker's project (once per lane: a repeat answers the finished seed) and
 * writes its ids to the project's seed file, which also marks the run as one whose global teardown checks that every
 * covered id was measured. `VOLUME_COUNTS` (JSON, e.g. '{"chat_per_kind":100}') shrinks the data set while a spec is
 * developed; such a run always ends red (global teardown).
 */
export async function seed(project: string): Promise<VolumeData> {
  const counts = JSON.parse(process.env.VOLUME_COUNTS ?? '{}') as Record<string, number>;
  await laneReady(laneOf(project));
  await director('POST', '/volume/seed', { counts });
  let state: SeedState = { state: 'running' };
  await expect
    .poll(
      async () => {
        state = (await director('GET', '/volume/seed')) as SeedState;
        return state.state;
      },
      { timeout: 25 * 60_000, intervals: [2000] },
    )
    .not.toBe('running');
  if (state.error !== undefined || !state.data) throw new Error(`volume seed failed: ${state.error ?? 'no data'}`);
  const data = state.data;
  const how = data.cached ? 'from the cache, seeded in' : 'seeded in';
  console.log(
    `KOR-35 ${project} data set ${how} ${data.seconds} s, ${JSON.stringify(data.timings)}, ` +
      `counts ${JSON.stringify(data.counts)}`,
  );
  const file = seedFile(WEB_DIR, project);
  mkdirSync(dirname(file), { recursive: true });
  // an override is for experiments only: the global teardown fails a run whose seed was shrunk
  writeFileSync(file, JSON.stringify({ ...data, override: counts }, null, 2));
  return data;
}

/** Live load: the live sessions write `rate` events per second for `seconds` (background, returns at once). */
export async function liveLoad(seconds: number, rate: number): Promise<void> {
  await director('POST', '/volume/live', { seconds, rate });
}

/** Live events the device double has stored and announced since the hub started. */
export async function liveWritten(): Promise<number> {
  return ((await director('GET', '/volume/live')) as { written: number }).written;
}
