// The volume data set, seeded once per Playwright run before the volume projects (ADR-086 §4): the device double
// builds it in the hub's database (`scripts/hub_test_volume.py`); the ids go to `test-results/.volume/seed.json`
// for the specs and mark the run as one whose global teardown checks that every covered id was measured.
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { expect, test } from '@playwright/test';
import { WEB_DIR } from '../fixtures/env.ts';
import { seedFile } from './completeness.ts';
import { director, type VolumeData } from './data.ts';

interface SeedState {
  readonly state: 'idle' | 'running' | 'done' | 'failed';
  readonly data?: VolumeData;
  readonly error?: string;
}

test('seeds the volume data set', async () => {
  test.setTimeout(20 * 60_000);
  // VOLUME_COUNTS (JSON, e.g. '{"chat_per_kind":100}') shrinks the data set while a spec is developed; such a run
  // always ends red (global teardown)
  const counts = JSON.parse(process.env.VOLUME_COUNTS ?? '{}') as Record<string, number>;
  await director('POST', '/volume/seed', { counts });
  let state: SeedState = { state: 'running' };
  await expect
    .poll(
      async () => {
        state = (await director('GET', '/volume/seed')) as SeedState;
        return state.state;
      },
      { timeout: 19 * 60_000, intervals: [2000] },
    )
    .not.toBe('running');
  expect(state.error ?? null).toBeNull();
  const data = state.data!;
  console.log(`KOR-35 seed: ${data.seconds} s, ${JSON.stringify(data.timings)}, counts ${JSON.stringify(data.counts)}`);
  const file = seedFile(WEB_DIR);
  mkdirSync(dirname(file), { recursive: true });
  // an override is for experiments only: the global teardown fails a run whose seed was shrunk
  writeFileSync(file, JSON.stringify({ ...data, override: counts }, null, 2));
});
