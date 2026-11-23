// E2E-14 (07-testing.md section 2.1; 03-api.md sections 5, 6.1, 6.2; ADR-013): offline and reconnect. During a running
// turn the link runner ↔ hub is cut (relay of the orchestrator) and so is the browser's live stream. The session keeps
// working: every event is stored in the runner's outbox at once, which grows gapless while nothing reaches the hub.
// The runner link comes back first: the outbox is pushed and acknowledged in order, nothing twice, no
// `origin_seq_conflict`. Then the browser link comes back and the chat fills the gap: all outputs in order, once.
// Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import {
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  uuid,
} from '../fixtures/platform.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-14-offline-reconnect.json', import.meta.url), 'utf8'),
);
const OUTPUTS = [
  'Step one kz14-out-1-a7s2.',
  'Step two kz14-out-2-a7s2.',
  'Step three kz14-out-3-a7s2.',
  'Step four kz14-out-4-a7s2.',
  'Step five kz14-out-5-a7s2.',
  'All steps done kz14-answer-q1w9.',
];
const CANARIES = [
  'kz14-prompt-z8x3c6v1',
  'kz14-out-3-a7s2',
  'kz14-answer-q1w9',
  'kz14-file-three',
  'kz14-name-b2n5m8k4',
];

interface Outbox {
  readonly n: number;
  readonly max: number;
  readonly pending: number;
}

const outbox = async (orchestrator: Orchestrator): Promise<Outbox> =>
  (
    await runnerStore<Outbox>(
      orchestrator,
      `SELECT count(*)::int AS n, coalesce(max(origin_seq), 0)::int AS max,
              (count(*) FILTER (WHERE pushed_at IS NULL))::int AS pending FROM runner.outbox`,
    )
  )[0]!;

const hubOutputs = async (orchestrator: Orchestrator, session: string): Promise<number> =>
  (
    await orchestrator.hubQuery<{ n: number }>(
      `SELECT count(*)::int AS n FROM hub.event WHERE session_id = '${uuid(session)}' AND kind = 'session.output'`,
    )
  )[0]!.n;

const hubAcked = async (orchestrator: Orchestrator, device: string): Promise<number> =>
  (
    await orchestrator.hubQuery<{ seq: number }>(
      `SELECT coalesce(max(last_origin_seq), 0)::int AS seq FROM hub.device_stream WHERE device_id = '${uuid(device)}'`,
    )
  )[0]!.seq;

test.describe.configure({ mode: 'serial' });

test('@E2E-14 @R-6.1 @R-6.2 @R-6.8 @R-6.9 @R-14.6 @R-P.5 @needs-vm a turn continues offline; the outbox and the browser catch up in order without duplicates', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  // The platform device serves every spec of a run: its audit rows count from here on, for this org or none.
  const since = (await orchestrator.hubQuery<{ since: string }>(`SELECT now()::text AS since`))[0]!.since;
  await orchestrator.loadScenario('model', scenario);
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz14-name-b2n5m8k4');
  const task = await sendPrompt(page, 'E2E14-OFFLINE kz14-prompt-z8x3c6v1: write five files, one by one');
  let session = '';
  await expect.poll(async () => (session = (await mainSessions(orchestrator, task))[0]?.id ?? '')).not.toBe('');

  // Cut both links as soon as the first output reached the hub.
  await expect.poll(() => hubOutputs(orchestrator, session), { timeout: 90_000 }).toBeGreaterThanOrEqual(1);
  await orchestrator.cut('runner-hub');
  await orchestrator.cut('browser-hub');
  try {
    const atCut = await outbox(orchestrator);
    const outputsAtCut = await hubOutputs(orchestrator, session);
    expect(outputsAtCut).toBeLessThan(OUTPUTS.length);

    // Offline: the turn goes on, the outbox grows (stored locally at once, gapless), the hub gets nothing.
    await expect
      .poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 90_000, intervals: [1000] })
      .toBe(true);
    await expect
      .poll(async () => (await outbox(orchestrator)).pending, { timeout: 30_000 })
      .toBeGreaterThan(atCut.pending);
    const offline = await outbox(orchestrator);
    expect(offline.n).toBe(offline.max);
    expect(offline.max).toBeGreaterThan(atCut.max);
    expect(await hubOutputs(orchestrator, session)).toBe(outputsAtCut);
    expect(await hubAcked(orchestrator, device)).toBeLessThan(offline.max);
    const links = await orchestrator.links();
    expect(links['runner-hub']!.cut && links['browser-hub']!.cut).toBe(true);

    // Runner link back: everything pushed and acknowledged in order, nothing twice, syncing not stopped.
    await orchestrator.restore('runner-hub');
    await expect
      .poll(async () => (await outbox(orchestrator)).pending, { timeout: 120_000, intervals: [1000] })
      .toBe(0);
    const final = await outbox(orchestrator);
    expect(final.n).toBe(final.max);
    // The turn may still append an item after the poll above; the hub stores only gapless (`origin_seq_gap`), so
    // reaching at least the outbox head read here proves that nothing written so far was lost.
    await expect
      .poll(async () => hubAcked(orchestrator, device), { timeout: 30_000, intervals: [500] })
      .toBeGreaterThanOrEqual(final.max);
    const stored = await orchestrator.hubQuery<{ origin_seq: number; scope_seq: number; scope_id: string }>(
      `SELECT origin_seq::int, scope_seq::int, scope_id FROM hub.event WHERE origin_device_id = '${uuid(device)}'
        ORDER BY scope_id, scope_seq`,
    );
    expect(new Set(stored.map((row) => row.origin_seq)).size).toBe(stored.length);
    for (let i = 1; i < stored.length; i++)
      if (stored[i]!.scope_id === stored[i - 1]!.scope_id)
        expect(stored[i]!.origin_seq).toBeGreaterThan(stored[i - 1]!.origin_seq);
    expect(
      await runnerStore(orchestrator, `SELECT key, value FROM runner.sync_state WHERE key = 'sync_fatal'`),
    ).toEqual([]);
    expect((await orchestrator.runners())['runner-1']).toBe('running');
    expect(
      await orchestrator.hubQuery(
        `SELECT action FROM hub.audit WHERE action LIKE 'runner.%' AND device_id = '${uuid(device)}'
          AND at >= '${since}' AND (org_id IS NULL OR org_id = '${uuid(onboarded.org)}')`,
      ),
    ).toEqual([]);

    // Browser link back: the chat fills the gap, every output once and in order.
    await orchestrator.restore('browser-hub');
    await expect(page.getByTestId('chat.output')).toHaveText(
      OUTPUTS.map((text) => new RegExp(`${text.replace('.', '\\.')}$`)),
      {
        timeout: 90_000,
      },
    );
    await expect(page.getByTestId('chat.file.path')).toHaveCount(5);
    // The stored events open with the user's keys: the six outputs, each once, in order.
    expect((await taskBodies(page, orchestrator, onboarded, task, 'session.output')).map((body) => body.text)).toEqual(
      OUTPUTS,
    );
  } finally {
    await orchestrator.restore('runner-hub');
    await orchestrator.restore('browser-hub');
  }

  await expectNoPlaintext(orchestrator, CANARIES);
});
