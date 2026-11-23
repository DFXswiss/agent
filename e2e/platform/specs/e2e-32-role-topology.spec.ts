// E2E-32 (07-testing.md sections 2.1, 5.2; R-3.13, R-P.7, R-P.8): the role topology. Hub, its PostgreSQL, the browser
// and the orchestrator run on the second device; the runner, its VMs and the doubles on the runner device; the two
// devices talk over the LAN only. The full flows of E2E-06 (first task under 10 s on the warm pool) and E2E-21 (GitHub
// operation chain) run across the two devices, then the egress report of both devices (`GET /egress/report`,
// `orchestrator/egress_report.py`) proves: the hub listens on a private LAN address and is reached under a loopback
// origin (no public name), the runner device reaches the hub only through its relay to exactly that address and port,
// every other connection of the run's processes stays on the device (doubles, store, VMs) or is the guest OS's time
// and name service, no relay targets anything else, no process of the run is a tunnel. The report goes to the
// iteration's artefacts (`e2e-32/egress-report.json`). T2 only.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test } from '../fixtures/test.ts';
import { firstTask, githubChain } from '../fixtures/flows.ts';
import { GITHUB_SITE } from '../fixtures/github.ts';
import { runnerStore } from '../fixtures/guest.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import { home, onboard, platformDevice, poolReady, uuid } from '../fixtures/platform.ts';

const scenario = (name: string): unknown =>
  JSON.parse(readFileSync(new URL(`../scenarios/${name}.json`, import.meta.url), 'utf8'));

interface EgressReport {
  readonly topology: string;
  readonly hub: { readonly origin: string; readonly address: string; readonly port: number; readonly lan: boolean };
  readonly devices: Readonly<
    Record<
      string,
      {
        readonly links: Readonly<Record<string, { target: string; connections: number }>>;
        readonly destinations: readonly { command: string; class: string; count: number }[];
        readonly tunnels: readonly string[];
      }
    >
  >;
  readonly problems: readonly string[];
}

/** The audit position before this spec: the shared platform device may carry audits of earlier specs. */
const auditHead = async (orchestrator: Orchestrator): Promise<number> =>
  (await orchestrator.hubQuery<{ seq: number }>(`SELECT coalesce(max(seq), 0)::int AS seq FROM hub.audit`))[0]!.seq;

/** The inbound refusals `device` audited after audit position `since`, once its outbox reached the hub. None in
 * normal operation: a new user's first request may arrive before the device's copy of the log shows the user, and
 * the device judges it only after refreshing its logs (KOR-17). */
async function inboundRejections(orchestrator: Orchestrator, device: string, since: number): Promise<unknown[]> {
  const pending = async () =>
    (
      await runnerStore<{ n: number }>(
        orchestrator,
        'SELECT count(*)::int AS n FROM runner.outbox WHERE pushed_at IS NULL',
      )
    )[0]!.n;
  await expect.poll(pending, { timeout: 60_000 }).toBe(0);
  return orchestrator.hubQuery(
    `SELECT target_kind, detail->>'code' AS code FROM hub.audit
      WHERE action = 'runner.inbound_rejected' AND device_id = '${uuid(device)}' AND seq > ${since}`,
  );
}

test.describe.configure({ mode: 'serial' });

test('@E2E-32 @R-3.13 @R-15.1 @R-15.2 @R-P.5 @R-P.7 @R-P.8 @needs-vm hub on the second device and runner on the runner device work over the LAN only', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.skip(e2e.topology !== 't2', 'the role topology needs the hub on the second device (T2)');
  test.setTimeout(900_000);
  const state = await orchestrator.state();
  expect(Object.keys(state.runners)).toEqual(['runner-1']);
  const device = await platformDevice(orchestrator);
  const since = await auditHead(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const ada = await onboard(page, orchestrator);

  // E2E-06 across the devices.
  await orchestrator.loadScenario('model', scenario('e2e-06-first-task'));
  await firstTask(page, orchestrator, device, 'What is 2+2?', '2+2 is 4.');

  // E2E-21 across the devices.
  await orchestrator.loadScenario('model', scenario('e2e-21-github-chain'));
  await home(page);
  await githubChain(page, orchestrator, ada, device, {
    login: 'kz21-octo-w3n7c5',
    repo: 'octo-org/hello',
    branch: 'e2e21/notes',
    prompt: 'E2E21-CHAIN kz21-prompt-x4b8m2s6: add notes to octo-org/hello and open a draft pull request',
    answer: `Draft pull request #1 is open: ${GITHUB_SITE}/octo-org/hello/pull/1 kz21-answer-z1f5k9m3.`,
    comment: 'Ready for review kz21-comment-t6j2h8q4',
  });

  // The egress report of both devices.
  const report = await orchestrator.call<EgressReport>('GET', '/egress/report');
  const dir = join(e2e.iterationDir, 'e2e-32');
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, 'egress-report.json'), JSON.stringify(report, null, 1));
  expect(report.problems).toEqual([]);
  expect(report.topology).toBe('t2');
  expect(report.hub.lan).toBe(true);
  expect(Object.keys(report.devices).sort()).toEqual(['hub-device', 'runner-device']);
  const runner = report.devices['runner-device']!;
  expect(runner.links['runner-hub']).toMatchObject({ target: `${report.hub.address}:${report.hub.port}` });
  expect(runner.links['runner-hub']!.connections).toBeGreaterThan(0);
  // the runner device reached the doubles (loopback) and the hub, the hub device the runner device's connections
  expect(Object.keys(runner.links)).toEqual(expect.arrayContaining(['model', 'github', 'runner-hub']));
  expect(runner.tunnels).toEqual([]);
  expect(report.devices['hub-device']!.tunnels).toEqual([]);
  // No inbound refusal of the device during the scenario (KOR-17).
  expect(await inboundRejections(orchestrator, device, since)).toEqual([]);

  expect(new URL(e2e.hubOrigin).hostname).toBe('localhost');
});
