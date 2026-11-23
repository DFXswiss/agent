// Delegated agent access in the browser (11-agent-access.md sections 5.1, 5.3, 7, 8; 06-web.md section 8.3) against
// the real hub: the `/connect` page of a pairing link (fragment removed, name, computer, age, full access, main
// orchestrator, warning), the pairing-tagged passkey gesture, the same-tab form POST to the agent's loopback listener
// (r never in a URL), the agent's claim, "Connected agents" in the settings; an agent cannot pair another one; a second
// pairing confirmed with the passkey names and replaces the first, which is revoked at once; the audit marker of an
// agent's action, the disconnect dialogs and the account-level notification; a forged fingerprint, an unknown
// pairing, a malformed link and the repeated gesture after `pairing_gesture_required`; the unlock without PRF.
import { openIdentity } from '../src/core/crypto/agent';
import { b64uDecode } from '../src/core/encoding/b64u';
import { uuid7 } from '../src/core/encoding/ids';
import { TEST_ROOT_KEY } from './fixtures/env.ts';
import { expect, test } from './fixtures/test.ts';
import { catchHandover, TestAgent } from './helpers/agent.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { cancelNext } from './helpers/cancel.ts';
import { onboard } from './helpers/onboard.ts';
import { PageUser } from './helpers/user.ts';

// at most 3 open pairings per source address (11-agent-access.md section 2): one test of this file at a time
test.describe.configure({ mode: 'default' });

test('connects the main orchestrator with one gesture; a second one replaces it; settings, audit, notification', async ({
  page,
  baseURL,
}) => {
  const origin = baseURL!;
  await addAuthenticator(page);
  const ada = await onboard(page, { origin });
  const user = await PageUser.open(page, ada.recoveryCode);
  const agent = await TestAgent.pair(origin);
  let claimed: { status: number; body: unknown } | null = null;
  const handover = catchHandover(page, origin, async (sent) => {
    claimed = await agent.claim(sent.fields.get('r')!, sent.fields.get('u')!);
  });

  await page.goto(`/connect#${await agent.fragment({ host: 'Büro-Mac' })}`);
  await expect(page).toHaveURL(/\/connect$/);
  await expect(page.getByTestId('connect.name')).toHaveText('Claude Code');
  await expect(page.getByTestId('connect.host')).toHaveText('Büro-Mac');
  await expect(page.getByTestId('connect.age')).toHaveText('Requested just now');
  await expect(page.getByTestId('connect.access')).toHaveText(
    'This agent receives full access to your account and can do everything you can.',
  );
  await expect(page.getByTestId('connect.main')).toContainText('It becomes your main orchestrator.');
  await expect(page.getByTestId('connect.warning')).toHaveText(
    'Only confirm if you have just asked your AI agent on this computer to connect.',
  );
  await page.getByTestId('connect.confirm').click();
  const sent = await handover;
  expect(sent.method).toBe('POST');
  expect(sent.navigation).toBe(true);
  expect([...sent.fields.keys()]).toEqual(['p', 'u', 'r', 'k']);
  expect(sent.fields.get('u')).toBe(user.userId);
  expect(sent.fields.get('p')).toBe(agent.id);
  expect(sent.fields.get('k')).toBe(TEST_ROOT_KEY);
  expect(b64uDecode(sent.fields.get('r'), 32)).toHaveLength(32);
  expect(claimed!.status).toBe(200);

  // the agent's redirect: a new page load, so the settings ask for the passkey again; the hint is only a hint
  await expect(page).toHaveURL(/\/login\?next=/);
  expect(page.url()).not.toContain(sent.fields.get('r')!);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('agents.hint.connected')).toBeVisible();
  const first = page.locator(`[data-testid="agents.item"][data-agent="${agent.id}"]`);
  await expect(page.getByTestId('agents.main').locator('[data-testid="agents.item"]')).toHaveAttribute(
    'data-agent',
    agent.id,
  );
  await expect(first).toHaveAttribute('data-state', 'active');
  await expect(first.getByTestId('agents.item.name')).toHaveText('Claude Code');
  await expect(first.getByTestId('agents.item.host')).toHaveText('on Büro-Mac');
  await expect(first.getByTestId('agents.item.replaces')).toHaveCount(0);
  await expect(first.getByTestId('agents.item.state')).toHaveText('Active');

  // the agent acts: it reads the org (audit "by agent"); it can never pair another agent itself
  expect((await agent.request('GET', `/v1/orgs/${ada.org}`)).status).toBe(200);
  const identity = await openIdentity(
    agent.keys.kem,
    b64uDecode(sent.fields.get('r')),
    agent.id,
    user.userId,
    (claimed!.body as { bundle_ct: string }).bundle_ct,
  );
  const second = await TestAgent.pair(origin);
  expect((await agent.confirm(second, user.userId, identity, 'Helper')).status).toBe(403);

  // the user confirms the second pairing with the passkey: the page names the agent it replaces and asks once more
  let claimedSecond: { status: number; body: unknown } | null = null;
  const handoverSecond = catchHandover(page, origin, async (handed) => {
    claimedSecond = await second.claim(handed.fields.get('r')!, handed.fields.get('u')!);
  });
  await page.goto(`/connect#${await second.fragment({ name: 'Helper' })}`);
  await expect(page.getByTestId('connect.name')).toHaveText('Helper');
  await page.getByTestId('connect.confirm').click();
  await expect(page.getByTestId('connect.replaces')).toHaveText(
    'Replaces “Claude Code”, your current main orchestrator. “Claude Code” is disconnected at once.',
  );
  await expect(page.getByTestId('connect.access')).toBeVisible();
  await page.getByTestId('connect.replace.confirm').click();
  expect((await handoverSecond).fields.get('p')).toBe(second.id);
  expect(claimedSecond!.status).toBe(200);
  // the replaced agent is revoked at once
  expect((await agent.request('GET', '/v1/agent/me')).status).toBe(401);

  await expect(page).toHaveURL(/\/login\?next=/);
  await page.getByTestId('login.submit').click();
  const helper = page.locator(`[data-testid="agents.item"][data-agent="${second.id}"]`);
  await expect(page.getByTestId('agents.main').locator('[data-testid="agents.item"]')).toHaveAttribute(
    'data-agent',
    second.id,
  );
  await expect(helper).toHaveAttribute('data-state', 'active');
  await expect(helper.getByTestId('agents.item.replaces')).toHaveText('Replaced Claude Code');
  await expect(page.getByTestId('agents.history').locator(`[data-agent="${agent.id}"]`)).toHaveAttribute(
    'data-state',
    'revoked',
  );
  await expect(first.getByTestId('agents.item.disconnect')).toHaveCount(0);
  // the account audit marks what the first agent did
  await expect(
    page.getByTestId('agents.activity.row.who').filter({ hasText: 'by agent Claude Code' }).first(),
  ).toBeVisible();
  await expect(page.getByTestId('agents.activity.row.who').filter({ hasText: 'by you' }).first()).toBeVisible();

  await page.goto(`/o/${ada.org}/audit`);
  await page.getByTestId('login.submit').click();
  const marked = page.locator('[data-testid="audit.row"][data-action="read.org"] [data-testid="audit.row.agent"]');
  await expect(marked.first()).toHaveText('by agent Claude Code');

  // the dialogs say what happens
  await page.getByTestId('shell.settings').click();
  await helper.getByTestId('agents.item.disconnect').click();
  await expect(page.getByTestId('agents.dialog.body')).toHaveText(
    'The agent loses all access at once. What it has already read stays with it.',
  );
  await page.getByTestId('agents.dialog.cancel').click();
  await expect(page.getByTestId('agents.dialog')).toHaveCount(0);
  await page.getByTestId('agents.disconnect-all').click();
  await expect(page.getByTestId('agents.dialog.body')).toHaveText(
    'Every agent loses all access at once. What they have already read stays with them.',
  );
  await page.getByTestId('agents.dialog.cancel').click();

  // the main orchestrator disconnects itself: the user is notified (account-level notification)
  expect((await second.request('DELETE', `/v1/me/agents/${second.id}`)).status).toBe(200);
  expect((await second.request('GET', '/v1/agent/me')).status).toBe(401);
  await page.goto('/notifications');
  await page.getByTestId('login.submit').click();
  const notice = page.locator('[data-testid="notifications.item"][data-kind="agent.account_change"]').first();
  await expect(notice).toHaveText(/Agent “Helper” disconnected an agent/);
  await notice.click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(helper).toHaveAttribute('data-state', 'revoked');
  await expect(page.getByTestId('agents.main.none')).toBeVisible();
  await expect(page.getByTestId('agents.disconnect-all')).toHaveCount(0);
});

test('a forged fingerprint, an unknown pairing and a malformed link are refused', async ({ page, baseURL }) => {
  const agent = await TestAgent.pair(baseURL!);
  await page.goto(`/connect#${await agent.fragment({ forged: true })}`);
  await expect(page.getByTestId('connect.error')).toHaveAttribute('data-code', 'pairing_mismatch');
  await expect(page.getByTestId('connect.error')).toContainText('This link is not valid');
  await expect(page.getByTestId('connect.confirm')).toHaveCount(0);

  const unknown = (await agent.fragment()).replace(agent.id, uuid7());
  await page.goto('about:blank');
  await page.goto(`/connect#${unknown}`);
  await expect(page.getByTestId('connect.error')).toHaveAttribute('data-code', 'pairing_unknown');
  await expect(page.getByTestId('connect.error')).toContainText('Connection request expired');

  await page.goto('about:blank');
  await page.goto('/connect#p=nothing');
  await expect(page.getByTestId('connect.invalid')).toContainText('This link is not valid');
  await page.goto('about:blank');
  await page.goto('/connect');
  await expect(page.getByTestId('connect.invalid')).toBeVisible();
});

test('offers the gesture again after pairing_gesture_required and a cancelled gesture', async ({ page, baseURL }) => {
  const origin = baseURL!;
  await addAuthenticator(page);
  await onboard(page, { origin });
  const agent = await TestAgent.pair(origin);
  const handover = catchHandover(page, origin);
  await cancelNext(page, 'get');
  await page.goto(`/connect#${await agent.fragment()}`);

  await page.getByTestId('connect.confirm').click();
  await expect(page.getByTestId('connect.notice')).toHaveText('Not confirmed. Try again when you are ready.');

  // the next ceremony loses its pairing tag on the way (as if more than 300 s had passed): the hub refuses the
  // confirmation of an untagged session with `pairing_gesture_required`
  let stripped = 0;
  await page.route('**/v1/passkeys/login/begin', async (route) => {
    if (stripped > 0) return route.fallback();
    stripped += 1;
    return route.fallback({ postData: '{}' });
  });
  await page.getByTestId('connect.confirm').click();
  await expect(page.getByTestId('connect.notice')).toHaveText(
    'The confirmation took too long. Confirm once more with your passkey.',
  );
  await page.getByTestId('connect.confirm').click();
  expect((await handover).fields.get('p')).toBe(agent.id);
  expect(stripped).toBe(1);
});

test('without PRF the recovery code unlocks before the confirmation', async ({ page, baseURL }) => {
  const origin = baseURL!;
  await addAuthenticator(page, { hasPrf: false });
  const ada = await onboard(page, { origin });
  const agent = await TestAgent.pair(origin);
  const handover = catchHandover(page, origin);
  await page.goto(`/connect#${await agent.fragment()}`);
  await page.getByTestId('connect.confirm').click();
  await page.getByTestId('prf-missing.recovery.input').fill(ada.recoveryCode);
  await page.getByTestId('prf-missing.recovery.submit').click();
  expect((await handover).fields.get('p')).toBe(agent.id);
});
