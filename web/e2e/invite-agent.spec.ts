// The invitation of the user's AI agent (11-agent-access.md section 5.2) against the real hub with a CDP virtual
// authenticator: the agent's link carries the invitation code, its pairing and the display name; the new user creates
// the passkey and confirms the sign-in – nothing else (no name field, no further button) – and the same tab hands
// over to the agent's loopback listener. The agent claims and is the user's main orchestrator; the hub holds the
// proposed name only encrypted. A pairing that does not match the link refuses the invitation before anything exists.
import { expect, test } from './fixtures/test.ts';
import { catchHandover, TestAgent } from './helpers/agent.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { platformInvite } from './helpers/platform.ts';

// at most 3 open pairings per source address (11-agent-access.md section 2): one test of this file at a time
test.describe.configure({ mode: 'default' });

test('a new user only creates the passkey: account, private org and the agent connected', async ({ page, baseURL }) => {
  const origin = baseURL!;
  const authenticator = await addAuthenticator(page);
  const sent: string[] = [];
  page.on('request', (request) => sent.push(`${request.url()}\n${request.postData() ?? ''}`));
  const invite = await platformInvite(origin);
  const agent = await TestAgent.pair(origin);
  let claimed: { status: number; body: unknown } | null = null;
  const handover = catchHandover(page, origin, async (handed) => {
    claimed = await agent.claim(handed.fields.get('r')!, handed.fields.get('u')!);
  });

  await page.goto(`/invite#${invite.code}&${await agent.fragment({ host: 'Büro-Mac' })}&u=Ada+Lovelace`);
  await expect(page.getByTestId('invite.agent.name')).toHaveText('Claude Code');
  await expect(page.getByTestId('invite.agent.host')).toHaveText('Büro-Mac');
  await expect(page.getByTestId('invite.agent.warning')).toBeVisible();
  await expect(page.getByTestId('invite.proposed-name')).toContainText('Ada Lovelace');
  await expect(page.getByTestId('invite.display-name')).toHaveCount(0);
  expect(new URL(page.url()).hash).toBe('');
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  const handed = await handover;
  expect(handed.navigation).toBe(true);
  expect(handed.fields.get('p')).toBe(agent.id);
  expect(claimed!.status).toBe(200);
  expect(await authenticator.credentials()).toHaveLength(1);
  // the sign-in after the new passkey was the pairing-tagged ceremony
  expect(sent.some((line) => line.includes('/v1/passkeys/login/begin') && line.includes(agent.id))).toBe(true);
  // the agent acts with the user's rights: the account has its private org
  const me = await agent.request('GET', '/v1/me');
  expect(me.status).toBe(200);
  expect((me.body as { orgs: unknown[] }).orgs).toHaveLength(1);
  // neither the invitation code nor the display name went to the hub in clear text
  const traffic = sent.filter((line) => !line.startsWith('http://127.0.0.1')).join('\n');
  for (const secret of [invite.code, invite.code.replace(/-/g, ''), 'Ada Lovelace']) {
    expect(traffic).not.toContain(secret);
  }

  await expect(page).toHaveURL(/\/login\?next=/);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('agents.main').locator('[data-testid="agents.item"]')).toHaveAttribute(
    'data-agent',
    agent.id,
  );
});

test('an invitation whose pairing does not match the link is refused before anything is created', async ({
  page,
  baseURL,
}) => {
  await addAuthenticator(page);
  const invite = await platformInvite(baseURL!);
  const agent = await TestAgent.pair(baseURL!);
  await page.goto(`/invite#${invite.code}&${await agent.fragment({ forged: true })}`);
  await expect(page.getByTestId('invite.invalid')).toHaveAttribute('data-code', 'pairing_mismatch');
  await expect(page.getByTestId('invite.create-passkey')).toHaveCount(0);
});
