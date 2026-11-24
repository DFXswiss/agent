// The volume data set of the run (seeded once by `seed.setup.ts` through the device double,
// `scripts/hub_test_volume.py`) and the follow-up calls of the volume specs.
import { readFileSync } from 'node:fs';
import { DIRECTOR_ORIGIN, WEB_DIR } from '../fixtures/env.ts';
import { seedFile } from './completeness.ts';

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
}

export async function director(method: 'GET' | 'POST', path: string, body?: unknown): Promise<unknown> {
  const response = await fetch(`${DIRECTOR_ORIGIN}${path}`, {
    method,
    headers: { 'content-type': 'application/json' },
    ...(body !== undefined && { body: JSON.stringify(body) }),
  });
  const data = (await response.json()) as unknown;
  if (!response.ok) throw new Error(`director ${method} ${path}: ${JSON.stringify(data)}`);
  return data;
}

let cached: VolumeData | null = null;

/** The ids of the data set (written by the seed project of this run). */
export function volumeData(): VolumeData {
  cached ??= JSON.parse(readFileSync(seedFile(WEB_DIR), 'utf8')) as VolumeData;
  return cached;
}

/** Live load: the live sessions write `rate` events per second for `seconds` (background, returns at once). */
export async function liveLoad(seconds: number, rate: number): Promise<void> {
  await director('POST', '/volume/live', { seconds, rate });
}

/** Live events the device double has stored and announced since the hub started. */
export async function liveWritten(): Promise<number> {
  return ((await director('GET', '/volume/live')) as { written: number }).written;
}
