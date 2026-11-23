// Org invitations on the invitation screen (06-web.md section 3.1, 04-crypto.md sections 5.1, 5.2, 5.2a) against the
// real hub with the virtual authenticator: a new user registers and joins (role, workgroups, `accept` with `inv_sig`,
// self-wraps, `member.profile`), an existing user joins with the existing account (locked session and from the intro
// after signing out), used and expired invitations are invalid, cancelled gestures return with "try again", and a
// manipulated lookup answer blocks the screen before anything is sent.
import type { Browser, Page } from '@playwright/test';
import { b64uDecode, b64uEncode } from '../src/core/encoding/b64u';
import { formatTime } from '../src/core/encoding/time';
import { formatCode, generateCode, inviteSecrets, parseCode, sealPackage } from '../src/core/crypto/invite';
import { buildEntries, type Entry, logText } from '../src/core/log/entries';
import { epochKey, ORG, WG } from '../src/core/log/state';
import { ed25519VerifyB64u } from '../src/core/crypto/keys';
import { extendLog } from '../src/core/log/verify';
import { expect, test } from './fixtures/test.ts';
import { addAuthenticator } from './helpers/authenticator.ts';
import { cancelNext } from './helpers/cancel.ts';
import { onboard, type Onboarded } from './helpers/onboard.ts';
import { PageUser } from './helpers/user.ts';

interface Admin {
  readonly page: Page;
  readonly onboarded: Onboarded;
  readonly user: PageUser;
}

async function newPage(browser: Browser, baseURL: string): Promise<Page> {
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();
  await addAuthenticator(page);
  return page;
}

async function admin(browser: Browser, baseURL: string, name = 'Ada Admin'): Promise<Admin> {
  const page = await newPage(browser, baseURL);
  const onboarded = await onboard(page, { origin: baseURL, name });
  return { page, onboarded, user: await PageUser.open(page, onboarded.recoveryCode) };
}

/**
 * An org invitation through the API, as the members screen builds it (`invite` entry and sealed package with the keys
 * of the org and the invited workgroups); returns the code.
 */
async function invite(
  ada: Admin,
  options: { wgs?: Record<string, 'read' | 'write'>; names?: Record<string, string>; expiresInMs?: number } = {},
): Promise<string> {
  const org = ada.onboarded.org;
  const log = await ada.user.log(org);
  const keys = await ada.user.keys(org, log);
  const code = generateCode();
  const secrets = await inviteSecrets(code);
  const wgs = options.wgs ?? {};
  const expires = formatTime(Date.now() + (options.expiresInMs ?? 72 * 3600_000));
  const [entry] = await buildEntries(
    { org, head: log.head },
    { id: ada.user.userId, key: ada.user.identity.sig.privateKey },
    [
      {
        type: 'invite',
        invite_id: secrets.inviteId,
        pk_inv: secrets.pkInv,
        kind: 'user',
        role: 'member',
        wgs,
        expires,
      },
    ],
    formatTime(Date.now()),
  );
  const after = await extendLog(log, [entry!]);
  const state = log.state;
  const scopes = [[ORG, org], ...[state.defaultWg, ...Object.keys(wgs)].map((wg) => [WG, wg])] as const;
  const config = (await (await ada.page.request.get('/v1/config')).json()) as {
    hub_key: { id: string; public_key: string };
  };
  const pkg = {
    org,
    org_name: 'Ada Labs',
    wg_names: { [state.defaultWg]: 'Default', ...options.names },
    founder: state.founder,
    head: after.head,
    keys: scopes.map(([scope, id]) => {
      const epoch = state.epoch(scope, id);
      return { scope, scope_id: id, epoch, key: keys.get(epochKey(scope, id, epoch))!.reveal(b64uEncode) };
    }),
    hub_key: config.hub_key,
    v: 1,
  };
  await ada.user.request('POST', `/v1/orgs/${org}/invites`, { entry, package_ct: await sealPackage(secrets, pkg) });
  return formatCode(code);
}

/** Display name, gesture 1, gesture 2, recovery code confirmed; returns the recovery code. */
async function registerAndJoin(page: Page, name: string): Promise<string> {
  await page.getByTestId('invite.display-name').fill(name);
  await page.getByTestId('invite.create-passkey').click();
  await page.getByTestId('invite.unlock').click();
  const recoveryCode = (await page.getByTestId('recovery-code.code').textContent())!;
  await page.getByTestId('recovery-code.confirm').fill(recoveryCode.slice(-6));
  await page.getByTestId('recovery-code.continue').click();
  return recoveryCode;
}

test('a new user joins an org with role, workgroups, both signatures and self-wraps @R-1.5 @R-1.4', async ({
  browser,
  baseURL,
}) => {
  const ada = await admin(browser, baseURL!);
  const org = ada.onboarded.org;
  const extra = await ada.user.createWorkgroup(org, 'Research');
  const code = await invite(ada, { wgs: { [extra]: 'read' }, names: { [extra]: 'Research' } });
  const page = await newPage(browser, baseURL!);
  await page.goto(`/invite#${code}`);
  // the fragment is gone from the address
  await expect(page).toHaveURL(/\/invite$/);
  await expect(page.getByTestId('invite.org-name')).toHaveText('You are invited to Ada Labs');
  await expect(page.getByTestId('invite.role')).toHaveText('Your role: Member');
  await expect(page.getByTestId('invite.workgroup')).toHaveText(['Default – read and write', 'Research – read']);
  await expect(page.getByTestId('invite.expires')).toContainText('The invitation is valid until');
  await page.getByTestId('invite.create-passkey').click();
  await expect(page.getByTestId('invite.display-name.invalid')).toBeVisible();
  const recoveryCode = await registerAndJoin(page, 'Erin Example');
  await expect(page).toHaveURL(new RegExp(`/o/${org}$`));
  // the hub's log: accept with sig and inv_sig, erin a member with read on the extra workgroup
  const log = await ada.user.log(org);
  const accept = log.entries[log.entries.length - 1]!;
  expect(accept.body).toMatchObject({ type: 'accept', kind: 'user' });
  const erin = accept.body.subject as string;
  const secrets = await inviteSecrets(parseCode(code));
  await ed25519VerifyB64u(b64uDecode(secrets.pkInv), accept.inv_sig, logText(accept.body));
  expect(log.state.member(erin)).toMatchObject({ role: 'member' });
  expect(log.state.wgAccess(erin, extra)).toBe('read');
  expect(log.state.wgAccess(erin, log.state.defaultWg)).toBe('write');
  // the org is the default context: a reload lands there after the sign-in
  await page.goto('/');
  await page.getByTestId('login.submit').click();
  await expect(page).toHaveURL(new RegExp(`/o/${org}$`));
  // erin's own wraps of the package keys (a second browser can unlock)
  const erinUser = await PageUser.open(page, recoveryCode);
  const { wraps } = (await erinUser.request('GET', '/v1/wraps')) as {
    wraps: { scope: string; scope_id: string; wrapped_by: string; recipient_id: string }[];
  };
  const own = wraps.filter((w) => w.recipient_id === erin && w.wrapped_by === erin && w.scope !== 'cred');
  expect(own.map((wrap) => `${wrap.scope} ${wrap.scope_id}`).sort()).toEqual(
    [`${ORG} ${org}`, `${WG} ${log.state.defaultWg}`, `${WG} ${extra}`].sort(),
  );
});

test('an existing user joins with the existing account and the used code is invalid afterwards @R-1.5', async ({
  browser,
  baseURL,
}) => {
  const ada = await admin(browser, baseURL!);
  const org = ada.onboarded.org;
  const bob = await admin(browser, baseURL!, 'Bob Builder');
  const code = await invite(ada);
  // a reload locks the session: the screen asks for the passkey right away
  await bob.page.goto(`/invite#${code}`);
  await expect(bob.page.getByTestId('invite.existing')).toHaveAttribute('data-mode', 'sign_in');
  await expect(bob.page.getByTestId('invite.org-name')).toHaveText('Join Ada Labs with your existing account');
  await bob.page.getByTestId('invite.sign-in').click();
  await expect(bob.page.getByTestId('invite.existing')).toHaveAttribute('data-mode', 'ready');
  await bob.page.getByTestId('invite.display-name').fill('Bob B.');
  await bob.page.getByTestId('invite.join').click();
  await expect(bob.page).toHaveURL(new RegExp(`/o/${org}$`));
  const log = await ada.user.log(org);
  const accept = log.entries[log.entries.length - 1]!;
  // no second identity: the accept carries bob's registered identity
  expect(accept.body).toMatchObject({ subject: bob.user.userId, pub: bob.user.identity.public() });
  // both orgs are listed for bob
  const me = (await bob.user.request('GET', '/v1/me')) as { orgs: { org_id: string }[] };
  expect(me.orgs.map((item) => item.org_id).sort()).toEqual([org, bob.onboarded.org].sort());
  // the code is used up
  const carol = await newPage(browser, baseURL!);
  await carol.goto(`/invite#${code}`);
  await expect(carol.getByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_unknown');
});

test('a signed-out user chooses "I already have an account" and a cancelled sign-in can be repeated', async ({
  browser,
  baseURL,
}) => {
  const ada = await admin(browser, baseURL!);
  const bob = await admin(browser, baseURL!, 'Bob Builder');
  const code = await invite(ada);
  await bob.page.goto('/settings');
  await bob.page.getByTestId('login.submit').click();
  await bob.page.getByTestId('settings.sign-out').click();
  await expect(bob.page).toHaveURL(/\/login$/);
  await cancelNext(bob.page, 'get');
  await bob.page.goto(`/invite#${code}`);
  await expect(bob.page.getByTestId('invite.org-intro')).toBeVisible();
  await bob.page.getByTestId('invite.existing-account').click();
  await bob.page.getByTestId('invite.back').click();
  await bob.page.getByTestId('invite.existing-account').click();
  await bob.page.getByTestId('invite.sign-in').click();
  await expect(bob.page.getByTestId('invite.notice')).toHaveText('Sign-in was cancelled – try again.');
  await bob.page.getByTestId('invite.sign-in').click();
  await bob.page.getByTestId('invite.display-name').fill('Bob');
  await bob.page.getByTestId('invite.join').click();
  await expect(bob.page).toHaveURL(new RegExp(`/o/${ada.onboarded.org}$`));
});

test('a cancelled passkey creation returns to the intro and an expired invitation is invalid', async ({
  browser,
  baseURL,
}) => {
  const ada = await admin(browser, baseURL!);
  const code = await invite(ada);
  const page = await newPage(browser, baseURL!);
  await cancelNext(page, 'create');
  await page.goto(`/invite#${code}`);
  await page.getByTestId('invite.display-name').fill('Grace');
  await page.getByTestId('invite.create-passkey').click();
  await expect(page.getByTestId('invite.notice')).toHaveText('Passkey creation was cancelled – try again.');
  await expect(page.getByTestId('invite.display-name')).toHaveValue('Grace');
  const expiring = await invite(ada, { expiresInMs: 3000 });
  await page.waitForTimeout(4000);
  // a new document (a fragment change alone is no navigation)
  await page.goto('/login');
  await page.goto(`/invite#${expiring}`);
  await expect(page.getByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_unknown');
});

test('a manipulated lookup answer blocks the screen and nothing is sent', async ({ browser, baseURL }) => {
  const ada = await admin(browser, baseURL!);
  // an entry between genesis and the invitation, whose signature the hub then forges
  await ada.user.createWorkgroup(ada.onboarded.org, 'Research');
  const code = await invite(ada);
  const page = await newPage(browser, baseURL!);
  const sent: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST' && !request.url().endsWith('/v1/invites/lookup')) sent.push(request.url());
  });
  await page.route('**/v1/invites/lookup', async (route) => {
    const response = await route.fetch();
    const body = (await response.json()) as { log: Entry[] };
    // the hub forges the signature of an entry after genesis
    body.log[1] = { ...body.log[1]!, sig: b64uEncode(new Uint8Array(64).fill(1)) };
    await route.fulfill({ response, json: body });
  });
  await page.goto(`/invite#${code}`);
  await expect(page.getByTestId('invite.blocked')).toHaveAttribute('data-code', 'log_signature');
  await expect(page.getByTestId('invite.blocked')).toContainText('This invitation cannot be trusted');
  expect(sent).toEqual([]);
});
