// Steps of the M2a scenarios "identity and membership" (SCN-03): further people in their own browser contexts, org
// invitations through the members screen (and through the API where a test needs an expiry the screen does not
// offer), device invitations through the device screen, further runner instances on the runner device
// (orchestrator/routes/runner_instances.py), the org log as a member reads it, and the events of a scope decrypted
// with an explicit key set (what a removed member or an unpaired device still holds).
import { expect, type Browser, type BrowserContext, type Page } from '@playwright/test';
import { b64uEncode } from '../../../web/src/core/encoding/b64u';
import { formatTime } from '../../../web/src/core/encoding/time';
import { formatCode, generateCode, inviteSecrets, sealPackage } from '../../../web/src/core/crypto/invite';
import { openEvent } from '../../../web/src/core/crypto/event';
import type { Secret } from '../../../web/src/core/crypto/secret';
import { buildEntries } from '../../../web/src/core/log/entries';
import { epochKey, ORG, WG } from '../../../web/src/core/log/state';
import { extendLog, verifyLog, type VerifiedLog } from '../../../web/src/core/log/verify';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { addAuthenticator, type Variant, type VirtualAuthenticator } from './authenticator.ts';
import type { E2eEnv } from './env.ts';
import type { Orchestrator } from './orchestrator.ts';
import { onboard, onboardWithCode, type Onboarded, uuid } from './platform.ts';

export interface Person {
  readonly context: BrowserContext;
  readonly page: Page;
  readonly authenticator: VirtualAuthenticator;
  readonly onboarded: Onboarded;
  readonly user: PageUser;
  readonly name: string;
}

/**
 * The hub's limit for anonymous requests (03-api.md section 1): 30 per minute and source address – a token bucket of
 * 30 that refills 0.5 per second. Every browser and the runners of a run reach the hub from the same address (T1:
 * all relays on the loopback), so the identity specs, which sign in, open invitation links, register and recover
 * many times, keep a mirror of the bucket and wait before a step whose anonymous requests would empty it. `RESERVE`
 * covers requests the mirror does not see (the onboarding fixture spaces itself, ACC-M1).
 */
const BUCKET = 30;
const REFILL_PER_S = 0.5;
const RESERVE = 10;
// Empty at the start of every Playwright worker: a worker restarted after a failed test does not know how much of the
// hub's bucket the specs before it used.
let bucketLevel = 0;
let bucketAt = Date.now();

/** Anonymous requests of the steps (GET /v1/config at every page load, lookups, passkey ceremonies). */
export const COST = { page: 1, link: 2, signIn: 3, register: 8, recover: 7, lookup: 1 } as const;

export async function anonymousBudget(cost: number): Promise<void> {
  const refill = () => {
    const now = Date.now();
    bucketLevel = Math.min(BUCKET, bucketLevel + ((now - bucketAt) / 1000) * REFILL_PER_S);
    bucketAt = now;
  };
  refill();
  const missing = cost + RESERVE - bucketLevel;
  if (missing > 0) {
    await new Promise((resolve) => setTimeout(resolve, (missing / REFILL_PER_S) * 1000));
    refill();
  }
  bucketLevel -= cost;
}

/** Opens the invitation link in a fresh document (a fragment change alone is no navigation). */
export async function openInvite(page: Page, code: string): Promise<void> {
  await anonymousBudget(COST.link + COST.page);
  await page.goto('/login');
  await page.goto(`/invite#${code}`);
}

/** Signs in on the login page or a locked screen with the page's authenticator. */
export async function signIn(page: Page, button = 'login.submit'): Promise<void> {
  await anonymousBudget(COST.signIn);
  await page.getByTestId(button).click();
}

/** A browser profile of its own with a virtual authenticator. */
export async function newBrowser(
  browser: Browser,
  e2e: E2eEnv,
  variant: Variant = 'platform',
): Promise<{ context: BrowserContext; page: Page; authenticator: VirtualAuthenticator }> {
  const context = await browser.newContext({ baseURL: e2e.hubOrigin });
  const page = await context.newPage();
  const authenticator = await addAuthenticator(page, variant);
  return { context, page, authenticator };
}

/** A new person: own browser profile, platform invitation of the operator, onboarding into a private org. */
export async function person(browser: Browser, e2e: E2eEnv, orchestrator: Orchestrator, name: string): Promise<Person> {
  const { context, page, authenticator } = await newBrowser(browser, e2e);
  await anonymousBudget(COST.register);
  const onboarded = await onboard(page, orchestrator, name);
  return { context, page, authenticator, onboarded, user: await PageUser.open(page, onboarded.recoveryCode), name };
}

/** The person of an already onboarded page (the test's default `page`). */
export async function personOf(
  page: Page,
  authenticator: VirtualAuthenticator,
  orchestrator: Orchestrator,
  name: string,
): Promise<Person> {
  await anonymousBudget(COST.register);
  const onboarded = await onboard(page, orchestrator, name);
  const user = await PageUser.open(page, onboarded.recoveryCode);
  return { context: page.context(), page, authenticator, onboarded, user, name };
}

/** Opens `path` with a reload: the identity is locked again and unlocks with the passkey (06-web.md section 2.3). */
export async function visit(page: Page, path: string): Promise<void> {
  await anonymousBudget(COST.page);
  await page.goto(path);
  await signIn(page);
  await expect(page).toHaveURL(new RegExp(`${path.replace(/[?]/g, '\\?')}$`), { timeout: 30_000 });
}

/** Client-side navigation (the router follows `popstate`; a reload would lock the identity). */
export async function navigate(page: Page, path: string): Promise<void> {
  await page.evaluate((target) => {
    history.pushState({}, '', target);
    dispatchEvent(new PopStateEvent('popstate'));
  }, path);
  await expect(page).toHaveURL(new RegExp(`${path}$`));
}

export const memberRow = (page: Page, name: string) => page.getByTestId('members.item').filter({ hasText: name });

/**
 * An org invitation through the members screen (06-web.md section 7.1): role and extra workgroup access; returns the
 * code shown once and checks the link shown with it.
 */
export async function inviteMember(
  page: Page,
  org: string,
  options: { readonly role?: 'member' | 'admin'; readonly wgs?: Readonly<Record<string, 'read' | 'write'>> } = {},
): Promise<string> {
  if (!page.url().endsWith(`/o/${org}/members`)) await navigate(page, `/o/${org}/members`);
  await expect(page.getByTestId('members.screen')).toBeVisible();
  await page.getByTestId('members.invite.open').click();
  // without an explicit role the screen's default is used – and must be `member` (R-8.3)
  if (options.role === undefined) await expect(page.getByTestId('members.invite.role')).toHaveValue('member');
  else await page.getByTestId('members.invite.role').selectOption(options.role);
  for (const [wg, access] of Object.entries(options.wgs ?? {}))
    await page.getByTestId(`members.invite.wg.${wg}`).selectOption(access);
  await page.getByTestId('members.invite.name').fill('Invited person');
  await page.getByTestId('members.invite.submit').click();
  await expect(page.getByTestId('members.invite.warning')).toBeVisible();
  const code = (await page.getByTestId('members.invite.code').textContent())!;
  await expect(page.getByTestId('members.invite.link')).toHaveText(`${new URL(page.url()).origin}/invite#${code}`);
  await page.getByTestId('members.invite.done').click();
  await expect(page.getByTestId('members.invite.result')).toHaveCount(0);
  return code;
}

/**
 * A user invitation (role member, default workgroup only) through the API exactly as the members screen builds it
 * (`invite` entry signed by the admin, package with the keys of the org and the default workgroup sealed with the
 * code) but with an expiry of `expiresInMs`: the screen offers hours only, the expiry test needs seconds (decision
 * SCN-03 1: the hub has no fake clock).
 */
export async function inviteByApi(
  admin: PageUser,
  org: string,
  expiresInMs: number,
): Promise<{ code: string; inviteId: string }> {
  const log = await admin.log(org);
  const keys = await admin.keys(org, log);
  const code = generateCode();
  const secrets = await inviteSecrets(code);
  const [entry] = await buildEntries(
    { org, head: log.head },
    { id: admin.userId, key: admin.identity.sig.privateKey },
    [
      {
        type: 'invite',
        invite_id: secrets.inviteId,
        pk_inv: secrets.pkInv,
        kind: 'user',
        role: 'member',
        wgs: {},
        expires: expiry(expiresInMs),
      },
    ],
    formatTime(Date.now()),
  );
  const after = await extendLog(log, [entry!]);
  const scopes = [
    [ORG, org],
    [WG, log.state.defaultWg],
  ] as const;
  const config = (await (await admin.page.request.get('/v1/config')).json()) as {
    hub_key: { id: string; public_key: string };
  };
  const pkg = {
    org,
    org_name: 'Private',
    wg_names: { [log.state.defaultWg]: 'Default' },
    founder: log.state.founder,
    head: after.head,
    keys: scopes.map(([scope, id]) => {
      const epoch = log.state.epoch(scope, id);
      return { scope, scope_id: id, epoch, key: keys.get(epochKey(scope, id, epoch))!.reveal(b64uEncode) };
    }),
    hub_key: config.hub_key,
    v: 1,
  };
  await admin.request('POST', `/v1/orgs/${org}/invites`, { entry, package_ct: await sealPackage(secrets, pkg) });
  return { code: formatCode(code), inviteId: secrets.inviteId };
}

function expiry(ms: number): string {
  return formatTime(Date.now() + ms);
}

/** The org invitation of a code through the invitation screen: a new person registers and joins (06 section 3.1). */
export async function joinAsNewUser(
  browser: Browser,
  e2e: E2eEnv,
  code: string,
  name: string,
): Promise<Omit<Person, 'onboarded'> & { readonly recoveryCode: string; readonly org: string }> {
  const { context, page, authenticator } = await newBrowser(browser, e2e);
  await anonymousBudget(COST.register + COST.link);
  const joined = await onboardWithCode(page, code, name);
  const user = await PageUser.open(page, joined.recoveryCode);
  return { context, page, authenticator, user, name, recoveryCode: joined.recoveryCode, org: joined.org };
}

/** Text of the hub's refusal of a signed request made by `user` (`<status> <body>`), or `ok`. */
export async function refusal(user: PageUser, method: string, target: string, body?: unknown): Promise<string> {
  try {
    await user.request(method, target, body);
    return 'ok';
  } catch (error) {
    const match = /: (\d{3}) (.*)$/s.exec(String((error as Error).message));
    if (match === null) throw error;
    const code = /"error":"([a-z_]+)"/.exec(match[2]!)?.[1] ?? match[2]!;
    return `${match[1]} ${code}`;
  }
}

/**
 * The org's log as `member` reads it: verified against the founder key of its genesis entry (the key the member's
 * invitation package named when it joined; the test trusts the hub's copy here).
 */
export async function memberLog(member: PageUser, org: string): Promise<VerifiedLog> {
  const page = (await member.request('GET', `/v1/orgs/${org}/log`)) as {
    entries: { entry: { body: { pub?: { sig: string } } } }[];
  };
  const entries = page.entries.map((item) => item.entry);
  return verifyLog(entries, entries[0]!.body.pub!.sig, await member.platform());
}

/**
 * The events of a scope (newest last) opened with `keys` – the keys a principal holds: an event is opened with the key
 * of its epoch; an event whose epoch key the principal does not hold is tried with every key of the scope it holds and
 * counts as `locked` (by its epoch) only when none of them opens it. The author keys come from the hub's tables (an
 * agent with its own identity authors under its own ID, ADR-082).
 */
export async function openScopeEvents(
  orchestrator: Orchestrator,
  reader: PageUser,
  scope: { readonly kind: 'org' | 'wg'; readonly id: string },
  keys: ReadonlyMap<string, Secret>,
  kinds: readonly string[],
): Promise<{ readonly opened: string[]; readonly locked: number[] }> {
  const listing = (await reader.request(
    'GET',
    `/v1/scopes/${scope.id}/events?kinds=${encodeURIComponent(kinds.join(','))}`,
  )) as { events: { event: { header: { id: string; epoch: number } } }[] };
  const authors = await orchestrator.hubQuery<{ id: string; sig_pub: string }>(
    `SELECT e.id, encode(coalesce(d.sig_pub, u.sig_pub, a.sig_pub), 'hex') AS sig_pub FROM hub.event e
       LEFT JOIN hub.device d ON d.id = e.author_id LEFT JOIN hub.user_identity u ON u.user_id = e.author_id
       LEFT JOIN hub.agent a ON a.id = e.author_id AND a.own_identity
      WHERE e.scope_id = '${uuid(scope.id)}'`,
  );
  const authorOf = new Map(authors.map((author) => [author.id, hex(author.sig_pub)]));
  const opened: string[] = [];
  const locked: number[] = [];
  for (const item of listing.events) {
    const header = item.event.header;
    const author = authorOf.get(header.id)!;
    const own = keys.get(epochKey(scope.kind, scope.id, header.epoch));
    const candidates = own !== undefined ? [own] : [...scopeKeys(keys, scope)];
    let plain: Uint8Array | null = null;
    for (const key of candidates) {
      plain = await openEvent(key, item.event, author).catch(() => null);
      if (plain !== null) break;
    }
    if (plain === null && own !== undefined) throw new Error(`event ${header.id} does not open with its epoch key`);
    if (plain === null) locked.push(header.epoch);
    else opened.push(new TextDecoder().decode(plain));
  }
  return { opened, locked };
}

function* scopeKeys(keys: ReadonlyMap<string, Secret>, scope: { kind: string; id: string }): Iterable<Secret> {
  for (const [name, key] of keys) if (name.startsWith(`${scope.kind} ${scope.id} `)) yield key;
}

export function hex(text: string): Uint8Array {
  return Uint8Array.from(text.match(/../g)!.map((pair) => parseInt(pair, 16)));
}

// ---------------------------------------------------------------------------------------------- runner instances

export interface Spawned {
  readonly name: string;
  readonly state: string;
  readonly vm_prefix: string;
  readonly home: string;
}

/**
 * A further runner instance `runner-<n>` (n above every runner the run has, `E2E_RUNNERS` included) with its own home
 * on the runner device, waiting for enrollment.
 */
export async function spawnRunner(
  orchestrator: Orchestrator,
  options: {
    readonly device?: 'platform' | 'org';
    readonly memory_reserve_mib?: number;
    readonly pool_size?: number;
    readonly session_idle_seconds?: number;
    readonly vm_idle_seconds?: number;
    /** A pool of the operator's subscriptions (one token file; which orgs use it comes from the platform log). */
    readonly operator_subscriptions?: boolean;
  } = {},
): Promise<Spawned> {
  const taken = Object.keys(await orchestrator.runners()).map((name) => Number(/^runner-(\d+)$/.exec(name)?.[1] ?? 0));
  const name = `runner-${Math.max(...taken) + 1}`;
  return orchestrator.call<Spawned>('POST', `/runners/${name}/spawn`, options);
}

/** Stops a spawned runner, deletes its VMs and its home. */
export const removeRunner = (orchestrator: Orchestrator, name: string) =>
  orchestrator.call<{ name: string; deleted: string[] }>('POST', `/runners/${name}/remove`, {});

/** `agent-runner doctor` / `status [--json]` of a runner instance. */
export const runnerCli = (orchestrator: Orchestrator, name: string, args: readonly string[]) =>
  orchestrator.call<{ rc: number; stdout: string; stderr: string }>('POST', `/runners/${name}/cli`, { args });

/** `agent-runner enroll --code` (org device) or `--platform`; returns the device ID it printed. */
export async function enrollRunner(
  orchestrator: Orchestrator,
  runner: string,
  code: string,
  platform = false,
): Promise<string> {
  const enrolled = await enrollAttempt(orchestrator, runner, code, platform);
  expect(enrolled.rc, enrolled.stderr).toBe(0);
  const id = /device_id=([0-9a-f-]{36})/.exec(enrolled.stdout)?.[1];
  if (id === undefined) throw new Error(`no device ID in: ${enrolled.stdout}`);
  return id;
}

/** One `agent-runner enroll` (its anonymous invitation lookup counts against the hub's limit). */
export async function enrollAttempt(orchestrator: Orchestrator, runner: string, code: string, platform = false) {
  await anonymousBudget(COST.lookup);
  return orchestrator.enroll(runner, code, platform);
}

/** The hub's view of a device: state and whether it holds a live connection. */
export async function deviceRow(
  orchestrator: Orchestrator,
  device: string,
): Promise<{ state: string; online: boolean; kind: string } | undefined> {
  const [row] = await orchestrator.hubQuery<{ state: string; online: boolean; kind: string }>(
    `SELECT d.state, d.kind, p.device_id IS NOT NULL AS online FROM hub.device d
       LEFT JOIN hub.device_presence p ON p.device_id = d.id WHERE d.id = '${uuid(device)}'`,
  );
  return row;
}

/** Waits until the hub sees the device in `state` and connected (`online`) or not. */
export async function expectDevice(
  orchestrator: Orchestrator,
  device: string,
  expected: { readonly state?: string; readonly online?: boolean },
  timeout = 90_000,
): Promise<void> {
  await expect.poll(async () => deviceRow(orchestrator, device), { timeout, intervals: [500] }).toMatchObject(expected);
}

/** Creates a device invitation through the device screen (default workgroup ticked); returns the code. */
export async function addDeviceInvitation(page: Page, org: string, wgs: readonly string[] = []): Promise<string> {
  if (!page.url().endsWith(`/o/${org}/devices`)) await navigate(page, `/o/${org}/devices`);
  await page.getByTestId('devices.add.open').click();
  for (const wg of wgs) await page.getByTestId(`devices.add.wg.${wg}`).check();
  await page.getByTestId('devices.add.submit').click();
  await expect(page.getByTestId('devices.add.warning')).toBeVisible();
  const code = (await page.getByTestId('devices.add.code').textContent())!;
  await expect(page.getByTestId('devices.add.command')).toHaveText(
    `agent-runner enroll --hub ${new URL(page.url()).origin} --code ${code}`,
  );
  return code;
}

export const deviceItem = (page: Page, id: string) => page.locator(`[data-testid="devices.item"][data-device="${id}"]`);

let egressSeen = 0;

/**
 * The closing checks of a spec (07-testing.md section 2.1): no egress except to the doubles so far, the model double
 * without failures (and with every expected request consumed when the spec loaded a scenario), the hub audit chain
 * verifies. The run repeats them for the whole iteration.
 */
export async function closingChecks(orchestrator: Orchestrator, scenario = false): Promise<void> {
  if (scenario)
    await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  // every double of the run (model; GitHub and mail where the run starts them): no failure, no open expectation
  for (const name of Object.keys((await orchestrator.state()).doubles)) {
    const state = await orchestrator.doubleState(name);
    expect(state.failures, `${name} double`).toEqual([]);
    if (name !== 'model') expect(state.ok, `${name} double`).toBe(true);
  }
  // egress: the violations recorded since the previous spec's closing checks belong to this spec
  const egress = await orchestrator.call<{ violations: unknown[] }>('GET', '/checks/egress');
  const fresh = egress.violations.slice(egressSeen);
  egressSeen = egress.violations.length;
  expect(fresh).toEqual([]);
  const audit = await orchestrator.call<{ rc: number; stdout: string }>('POST', '/checks/audit');
  expect(audit.rc, audit.stdout).toBe(0);
}
