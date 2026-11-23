// E2E-20 (07-testing.md section 2.1; 03-api.md sections 4.8, 4.10; 05-runner.md section 11.4; ADR-015): GitHub connect
// and disconnect. The user connects from the composer's GitHub chip: the `github.connect` device request reaches the
// platform device, which runs the device flow at the GitHub double; the page shows the user code with the link and the
// countdown; once the code is authorized the device stores the credential under the user's CK (the hub holds only
// ciphertext, the device a verified cache) and the page shows the login it decrypted. Disconnecting hard-deletes the
// record at the hub and the device purges its cache; a later operation of a session ends `github_not_connected`
// without a call to GitHub. Runs on T1 and T2.
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { auditRows, expectNoPlaintext, runnerStore } from '../fixtures/guest.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  expectGitHubOk,
  githubRoutes,
  hubOperations,
  operationAudit,
  repository,
  seedGitHub,
  worldOf,
  connectGitHub,
} from '../fixtures/github.ts';
import {
  expectAnswerInChat,
  home,
  hubTablesContaining,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';

const scenario: unknown = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-20-github-connect.json', import.meta.url), 'utf8'),
);
const LOGIN = 'kz20-octo-h5q9t2';
const PROMPT = 'E2E20-OP kz20-prompt-b7m3x8p1: which repositories can you see?';
const CANARIES = [LOGIN, 'kz20-prompt-b7m3x8p1', 'kz20-answer-r4c8w1n6', 'kz20-name-y2k6d9s4'];

interface CredentialRow {
  readonly kind: string;
  readonly version: number;
  readonly epoch: number;
  readonly ct_bytes: number;
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

test('@E2E-20 @R-9.1 @R-9.6 @R-P.5 @needs-vm GitHub connects through the device flow, disconnects everywhere and operations then end github_not_connected', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(480_000);
  await orchestrator.loadScenario('model', scenario);
  await seedGitHub(orchestrator, worldOf(LOGIN, 'octo-org', [repository('octo-org/hello')]));
  const device = await platformDevice(orchestrator);
  const since = await auditHead(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const onboarded = await onboard(page, orchestrator, 'Ada kz20-name-y2k6d9s4');
  const [owner] = await orchestrator.hubQuery<{ id: string }>(
    `SELECT principal_id AS id FROM hub.org_member WHERE org_id = '${uuid(onboarded.org)}' AND kind = 'user'`,
  );
  const user = owner!.id;

  // Connect: device code with the link to the double's device page and the countdown, then the login.
  const connected = await connectGitHub(page, orchestrator, LOGIN);
  expect(connected.verification).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/login\/device$/);
  const routes = await githubRoutes(orchestrator);
  expect(routes[0]).toBe('POST /login/device/code');
  expect(routes.slice(1, -1).every((route) => route === 'POST /login/oauth/access_token')).toBe(true);
  expect(routes.slice(-2)).toEqual(['POST /login/oauth/access_token', 'GET /user']);

  // The credential: CK-encrypted at the hub (written by the device), cached verified on the device.
  const credentials = await orchestrator.hubQuery<CredentialRow>(
    `SELECT kind, version, epoch, octet_length(ct) AS ct_bytes FROM hub.credential WHERE owner_id = '${uuid(user)}'`,
  );
  expect(credentials.map((row) => [row.kind, row.version, row.epoch])).toEqual([['github', 1, 1]]);
  expect(credentials[0]!.ct_bytes).toBeGreaterThan(0);
  expect(await hubTablesContaining(orchestrator, [LOGIN])).toEqual([]);
  const [put] = await orchestrator.hubQuery<{ actor_kind: string; actor_id: string }>(
    `SELECT actor_kind, actor_id FROM hub.audit WHERE action = 'credential.put' AND detail->>'kind' = 'github'
        AND target_id IN (SELECT id::text FROM hub.credential WHERE owner_id = '${uuid(user)}') ORDER BY seq`,
  );
  expect(put).toEqual({ actor_kind: 'device', actor_id: device });
  const cached = () =>
    runnerStore<{ kind: string }>(
      orchestrator,
      `SELECT kind FROM runner.credential_cache WHERE owner_id = '${uuid(user)}'::uuid`,
    );
  await expect.poll(cached, { timeout: 30_000 }).toEqual([{ kind: 'github' }]);

  // Disconnect: hard delete at the hub, purge on the device; the page links GitHub's settings.
  await page.getByTestId('github.disconnect').click();
  await page.getByTestId('github.disconnect.confirm').click();
  await expect(page.getByTestId('github.disconnected')).toBeVisible();
  await expect(page.getByTestId('github.settings-link')).toBeVisible();
  expect(await orchestrator.hubQuery(`SELECT id FROM hub.credential WHERE owner_id = '${uuid(user)}'`)).toEqual([]);
  const deleted = await orchestrator.hubQuery<{ actor_kind: string }>(
    `SELECT actor_kind FROM hub.audit WHERE action = 'credential.delete' AND detail->>'kind' = 'github' AND actor_id = '${uuid(user)}'`,
  );
  expect(deleted).toEqual([{ actor_kind: 'user' }]);
  await expect.poll(cached, { timeout: 60_000 }).toEqual([]);
  const callsBefore = (await githubRoutes(orchestrator)).length;

  // A later operation of a session: refused before any GitHub call.
  await home(page);
  const task = await sendPrompt(page, PROMPT);
  await expectAnswerInChat(page, 'GitHub is not connected kz20-answer-r4c8w1n6.');
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const [session] = await mainSessions(orchestrator, task);
  const operations = await hubOperations(orchestrator, task);
  expect(operations.map((op) => [op.session_id, op.type, op.state, op.error_code])).toEqual([
    [session!.id, 'github.list_repositories', 'rejected', 'github_not_connected'],
  ]);
  expect(await operationAudit(orchestrator, operations[0]!.id)).toContain('operation.rejected');
  expect((await auditRows(orchestrator, task, 'operation.rejected')).map((row) => row.detail.error_code)).toEqual([
    'github_not_connected',
  ]);
  await expect(page.getByTestId('operation.error')).toHaveAttribute('data-code', 'github_not_connected', {
    timeout: 30_000,
  });
  expect(await githubRoutes(orchestrator, callsBefore)).toEqual([]);

  // No inbound refusal of the device during the scenario (KOR-17).
  expect(await inboundRejections(orchestrator, device, since)).toEqual([]);

  await expectGitHubOk(orchestrator);
  await expectNoPlaintext(orchestrator, CANARIES);
});
