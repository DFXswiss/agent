// Delegated agent access in the browser (11-agent-access.md sections 5.1, 5.3, 7, 8; 06-web.md section 8.3) against
// the real hub: the `/connect` page of a pairing link (fragment removed, name, computer, age, full access, main
// orchestrator, warning), the pairing-tagged passkey gesture, the same-tab form POST to the agent's loopback listener
// (r never in a URL), the agent's claim, "Connected agents" in the settings; an agent cannot pair another one; a second
// pairing confirmed with the passkey names and replaces the first, which is revoked at once; the agent never receives
// the user's identity, only the user's public keys and its certificate, and is an agent of the user in the org's log
// (ADR-082); the replacement removes the first from the log and the second's own disconnect is cleaned up by the
// user's browser; the audit marker of an
// agent's action, the disconnect dialogs and the account-level notification; a forged fingerprint, an unknown
// pairing, a malformed link and the repeated gesture after `pairing_gesture_required`; the unlock without PRF; the
// agent's personal message (rule E-28) quoted as plain text, and nothing of it without one; a member without admin
// right replaces its orchestrator, rotating what the first one read in the org (ADR-082 section 4).
//
// Pairing an AI agent that runs on another computer (ADR-067, 11-agent-access.md section 5.4): the `m=remote` link,
// the warning that names the computer, the agent code typed by the user (a wrong code keeps the passkey button
// disabled; the page never shows the code), the confirmation with the handover sealed to the agent, the return code
// the user hands back, the agent fetching and checking the handover before it claims; the notification
// `agent_paired_remote`, the shell banner that marking notifications read does not hide, the settings marker and
// "Disconnect"; a mode that link and hub disagree on, both ways; the invitation that asks for the code before the
// passkey is created. These tests live in this file because pairings count against the limit of 3 open pairings per
// source (the tests of one file run one at a time, and each remote test claims every pairing it creates).
import { openUser } from '../src/core/crypto/agent';
import { Identity } from '../src/core/crypto/keys';
import { b64uDecode } from '../src/core/encoding/b64u';
import { uuid7 } from '../src/core/encoding/ids';
import { TEST_ROOT_KEY } from './fixtures/env.ts';
import { expect, test } from './fixtures/test.ts';
import { catchHandover, TestAgent } from './helpers/agent.ts';
import { addAuthenticator, withoutPrf } from './helpers/authenticator.ts';
import { cancelNext } from './helpers/cancel.ts';
import { joinWithCode } from './helpers/members.ts';
import { navigateInApp, onboard } from './helpers/onboard.ts';
import { platformInvite } from './helpers/platform.ts';
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
  // without a message nothing of it is shown (rule E-28)
  await expect(page.getByTestId('connect.message')).toHaveCount(0);
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

  // the agent got the user's public keys and its certificate, never the identity, and is the user's agent in the log
  const claimedBody = claimed!.body as Record<string, unknown>;
  expect(claimedBody).not.toHaveProperty('bundle_ct');
  expect(claimedBody.identity).toBe('own');
  const anchor = await openUser(
    agent.keys.kem,
    b64uDecode(sent.fields.get('r')),
    agent.id,
    user.userId,
    claimedBody.user_ct as string,
  );
  expect({ kem: anchor.kem, sig: anchor.sig }).toEqual(user.identity.public());
  expect((await user.log(ada.org)).state.activeAgent(user.userId)?.id).toBe(agent.id);
  // the agent writes on the org whiteboard with its own key: the page names it as the agent of its user
  expect(await agent.writeBoard(await user.log(ada.org), null, 'Noted by the agent')).toBe(200);
  await navigateInApp(page, `/o/${ada.org}/whiteboard`);
  const noted = page.getByTestId('whiteboard.item').filter({ hasText: 'Noted by the agent' });
  await expect(noted.getByTestId('whiteboard.item.author')).toHaveText('Agent of Ada Lovelace');
  await navigateInApp(page, '/settings');
  await expect(first.getByTestId('agents.item.runs')).toHaveText('Runs on its computer with its own key');

  // the agent acts: it reads the org (audit "by agent"); it can never pair another agent itself
  expect((await agent.request('GET', `/v1/orgs/${ada.org}`)).status).toBe(200);
  const second = await TestAgent.pair(origin);
  expect((await agent.confirm(second, user.userId, await Identity.generate(), 'Helper')).status).toBe(403);

  // the user confirms the second pairing with the passkey: the page names the agent it replaces and asks once more
  let claimedSecond: { status: number; body: unknown } | null = null;
  const handoverSecond = catchHandover(page, origin, async (handed) => {
    claimedSecond = await second.claim(handed.fields.get('r')!, handed.fields.get('u')!);
  });
  // the agent's personal message (rule E-28): quoted where the user approves, markup and an address only as text
  const message = 'Hi Ada, it is me: <b>Helper</b> <a href="https://evil.example">https://evil.example</a>';
  await page.goto(`/connect#${await second.fragment({ name: 'Helper', message })}`);
  await expect(page.getByTestId('connect.name')).toHaveText('Helper');
  await expect(page.getByTestId('connect.message')).toContainText('Personal message of the agent');
  await expect(page.getByTestId('connect.message.text')).toHaveText(message);
  await expect(page.getByTestId('connect.message').locator('a, b')).toHaveCount(0);
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
  // the replaced agent is out of the log, whose keys were replaced in the same batch
  const replaced = (await user.log(ada.org)).state;
  expect(replaced.activeAgent(user.userId)?.id).toBe(second.id);
  expect(replaced.member(agent.id)).toBeNull();
  expect(replaced.epoch('org', ada.org)).toBe(2);
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
    'The agent loses all access at once: it is removed from every organization and the keys it could read are ' +
      'replaced, so it cannot open anything new. What it has already read stays with it.',
  );
  await expect(page.getByTestId('agents.dialog.held')).toHaveCount(0);
  await page.getByTestId('agents.dialog.cancel').click();
  await expect(page.getByTestId('agents.dialog')).toHaveCount(0);
  await page.getByTestId('agents.disconnect-all').click();
  await expect(page.getByTestId('agents.dialog.body')).toHaveText(
    'Every agent loses all access at once: they are removed from every organization and the keys they could read ' +
      'are replaced, so they cannot open anything new. What they have already read stays with them.',
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
  // the user's browser removed the revoked agent from the log with its rotations (job "agents")
  await expect.poll(async () => (await user.log(ada.org)).state.member(second.id)).toBeNull();
  expect((await user.log(ada.org)).state.epoch('org', ada.org)).toBe(3);
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
  // a new account needs PRF (KOR-22): an existing account whose passkey signs in without PRF output
  const first = await addAuthenticator(page);
  const ada = await onboard(page, { origin });
  await withoutPrf(page, first);
  const agent = await TestAgent.pair(origin);
  const handover = catchHandover(page, origin);
  await page.goto(`/connect#${await agent.fragment()}`);
  await page.getByTestId('connect.confirm').click();
  await page.getByTestId('prf-missing.recovery.input').fill(ada.recoveryCode);
  await page.getByTestId('prf-missing.recovery.submit').click();
  expect((await handover).fields.get('p')).toBe(agent.id);
});

test('connects an agent on another computer with the two codes; notification, banner, settings, disconnect', async ({
  page,
  baseURL,
}) => {
  const origin = baseURL!;
  await addAuthenticator(page);
  const ada = await onboard(page, { origin });
  await PageUser.open(page, ada.recoveryCode);
  const agent = await TestAgent.pair(origin, { remote: true });
  const code = await agent.agentCode();

  // a remote pairing behind a loopback link is not valid
  await page.goto(`/connect#${(await agent.fragment()).replace('m=remote', 'c=40123')}`);
  await expect(page.getByTestId('connect.error')).toHaveAttribute('data-code', 'pairing_mismatch');
  await expect(page.getByTestId('connect.error')).toContainText('This link is not valid');

  // a new page load: the page reads the fragment once
  await page.goto('about:blank');
  await page.goto(`/connect#${await agent.fragment({ host: 'build-server' })}`);
  await expect(page).toHaveURL(/\/connect$/);
  await expect(page.getByTestId('connect.remote-warning')).toContainText(
    'This AI agent runs on another computer (build-server).',
  );
  await expect(page.getByTestId('connect.warning')).toHaveCount(0);
  await expect(page.getByTestId('connect.confirm')).toBeDisabled();
  expect(await page.locator('body').innerText()).not.toContain(code);
  await page.getByTestId('connect.code.input').fill('ABCDE-FGHJK');
  await expect(page.getByTestId('connect.code')).toContainText('This is not the code of this request.');
  await expect(page.getByTestId('connect.confirm')).toBeDisabled();
  await page.getByTestId('connect.code.input').fill(code.toLowerCase().replace('-', ' '));
  await expect(page.getByTestId('connect.confirm')).toBeEnabled();
  await page.getByTestId('connect.confirm').click();

  const returned = page.getByTestId('connect.return-code');
  await expect(returned).toHaveText(/^[0-9A-Z]{4}-[0-9A-Z]{4}$/);
  await expect(page.getByTestId('connect.outcome.waiting')).toHaveText('Waiting for your agent…');
  // the agent refuses a wrong return code without claiming, then accepts the right one
  const shown = (await returned.textContent())!;
  const wrong = shown.startsWith('0') ? `1${shown.slice(1)}` : `0${shown.slice(1)}`;
  expect((await agent.finishRemote(wrong)).body).toBe('pairing_failed');
  const claimed = await agent.finishRemote(shown);
  expect(claimed.status).toBe(200);
  await expect(page.getByTestId('connect.outcome.connected')).toHaveText('Your agent is connected.');
  expect((await agent.request('GET', '/v1/agent/me')).status).toBe(200);
  // the handover is gone once claimed
  expect((await agent.request('GET', '/v1/agent/handover')).status).toBe(404);

  // every client sees the notification; marking it read does not hide the banner
  await page.goto('/notifications');
  await page.getByTestId('login.submit').click();
  const banner = page.getByTestId('shell.remote-agent');
  await expect(banner).toContainText('Your main orchestrator runs on another computer (build-server), connected');
  const notice = page.locator('[data-testid="notifications.item"][data-kind="agent.account_change"]').first();
  await expect(notice).toHaveText(/An AI agent on another computer \(“Claude Code”\) became your main orchestrator/);
  await notice.click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(banner).toBeVisible();
  const item = page.locator(`[data-testid="agents.item"][data-agent="${agent.id}"]`);
  await expect(item.getByTestId('agents.item.remote')).toHaveText('Runs on another computer');
  await page.getByTestId('shell.remote-agent.disconnect').click();
  await expect(banner).toHaveCount(0);
  await expect(item).toHaveAttribute('data-state', 'revoked');
  expect((await agent.request('GET', '/v1/agent/me')).status).toBe(401);

  // a loopback pairing behind a remote link is not valid either; its own link then connects it (and closes it)
  const loopback = await TestAgent.pair(origin);
  await page.goto(`/connect#${(await loopback.fragment()).replace('c=40123', 'm=remote')}`);
  await expect(page.getByTestId('connect.error')).toHaveAttribute('data-code', 'pairing_mismatch');
  await page.goto('about:blank');
  let claimedLoopback: { status: number } | null = null;
  const handover = catchHandover(page, origin, async (sent) => {
    claimedLoopback = await loopback.claim(sent.fields.get('r')!, sent.fields.get('u')!);
  });
  await page.goto(`/connect#${await loopback.fragment()}`);
  await expect(page.getByTestId('connect.code')).toHaveCount(0);
  await page.getByTestId('connect.confirm').click();
  await handover;
  expect(claimedLoopback!.status).toBe(200);
});

test('an invitation asks for the agent code before the passkey is created', async ({ page, baseURL }) => {
  const origin = baseURL!;
  const authenticator = await addAuthenticator(page);
  const invite = await platformInvite(origin);
  const agent = await TestAgent.pair(origin, { remote: true });
  await page.goto(`/invite#${invite.code}&${await agent.fragment({ host: 'build-server' })}&u=Ada+Lovelace`);
  await expect(page.getByTestId('invite.agent.remote-warning')).toContainText('build-server');
  await expect(page.getByTestId('invite.create-passkey')).toBeDisabled();
  await page.getByTestId('invite.agent.code.input').fill('ABCDE-FGHJK');
  await expect(page.getByTestId('invite.create-passkey')).toBeDisabled();
  expect(await authenticator.credentials()).toHaveLength(0);
  await page.getByTestId('invite.agent.code.input').fill(await agent.agentCode());
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  const returned = page.getByTestId('connect.return-code');
  await expect(returned).toHaveText(/^[0-9A-Z]{4}-[0-9A-Z]{4}$/);
  await expect(page.getByTestId('connect.code')).toHaveCount(0);
  expect((await agent.finishRemote((await returned.textContent())!)).status).toBe(200);
  await expect(page.getByTestId('connect.outcome.connected')).toBeVisible();
  const me = await agent.request('GET', '/v1/me');
  expect((me.body as { orgs: unknown[] }).orgs).toHaveLength(1);
});

test('a member without admin right replaces its orchestrator; the batch of the org rotates what the first one read', async ({
  page,
  baseURL,
  newContext,
}) => {
  const origin = baseURL!;
  await addAuthenticator(page);
  const ada = await onboard(page, { origin, name: 'Ada Admin' });
  const admin = await PageUser.open(page, ada.recoveryCode);
  await page.getByTestId('shell.nav.members').click();
  await page.getByTestId('members.invite.open').click();
  await page.getByTestId('members.invite.name').fill('Invited person');
  await page.getByTestId('members.invite.submit').click();
  const code = (await page.getByTestId('members.invite.code').textContent())!;
  const bobPage = await (await newContext()).newPage();
  await addAuthenticator(bobPage);
  const bob = await onboard(bobPage, { origin, name: 'Bob Member' });
  const member = await PageUser.open(bobPage, bob.recoveryCode);
  await joinWithCode(member, origin, code, 'Bob Member');
  expect((await admin.log(ada.org)).state.isAdmin(member.userId)).toBe(false);

  const connect = async (agent: TestAgent, replaces: boolean) => {
    let status = 0;
    const handover = catchHandover(bobPage, origin, async (sent) => {
      status = (await agent.claim(sent.fields.get('r')!, sent.fields.get('u')!)).status;
    });
    await bobPage.goto(`/connect#${await agent.fragment()}`);
    await bobPage.getByTestId('connect.confirm').click();
    if (replaces) await bobPage.getByTestId('connect.replace.confirm').click();
    await handover;
    expect(status).toBe(200);
  };
  const first = await TestAgent.pair(origin);
  await connect(first, false);
  expect((await admin.log(ada.org)).state.activeAgent(member.userId)?.id).toBe(first.id);
  const second = await TestAgent.pair(origin);
  await connect(second, true);
  const state = (await admin.log(ada.org)).state;
  expect(state.activeAgent(member.userId)?.id).toBe(second.id);
  expect(state.member(first.id)).toBeNull();
  expect(state.epoch('org', ada.org)).toBe(2);
  expect(state.pendingScopes()).toEqual([]);
});
