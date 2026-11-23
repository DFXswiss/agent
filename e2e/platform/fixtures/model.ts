// Steps of the M2c model-access scenarios (SCN-05: E2E-16, -27, -28, -36): the model double's credential check per
// access kind, the hub's platform budget, the session environment and the credential scan (orchestrator/routes/
// model.py), the login doubles (routes/login.py), the subscription usage double (routes/quota.py) and the settings
// screens of model access and credentials (06-web.md section 8.2). Credential values the specs cannot know (the
// run's platform key, the tokens the login doubles issue) never leave the orchestrator.
import { randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { expect, type Browser, type Page } from '@playwright/test';
import { formatMoney, formatNumber } from '../../../web/src/core/i18n/format';
import { formatPercent } from '../../../web/src/core/monitoring/units';
import type { Orchestrator } from './orchestrator.ts';
import { uuid } from './platform.ts';

export type AccessKind = 'platform' | 'api_key' | 'subscription';
export type Preference = AccessKind | 'auto';

/** The credential the model double accepts: platform key (run secret), API key (`token`) or subscription token. */
export interface Access {
  readonly kind: AccessKind;
  readonly token?: string;
  readonly login_seed?: string;
}

/** Catalog models (contracts/model-catalog.example.json, the hub's and the runners' catalog in the run). */
export const PLATFORM_MODEL = 'deepseek/deepseek-v4.1-flash-1017';
export const CLAUDE_MODEL = 'claude-sonnet-5-5';
export const CONTEXT_WINDOW = { [PLATFORM_MODEL]: 1_048_576, [CLAUDE_MODEL]: 1_000_000 } as const;
/** Prices in USD per million tokens of the catalog (list price and subscription equivalent). */
export const SONNET_PRICE = { input: 2, output: 10, cache_read: 0.1, cache_write: 2.5 } as const;

/** Variables the session environment may hold. */
export const SESSION_ENV = [
  // set by systemd for the session unit (its own bookkeeping, nothing of the runner's or the guest API's environment)
  'INVOCATION_ID',
  'JOURNAL_STREAM',
  'LOGNAME',
  'MEMORY_PRESSURE_WATCH',
  'MEMORY_PRESSURE_WRITE',
  'SYSTEMD_EXEC_PID',
  'USER',
  // 05-runner.md section 10.3 and the session basics of the guest
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_AUTH_TOKEN',
  'ANTHROPIC_BASE_URL',
  'ANTHROPIC_DEFAULT_HAIKU_MODEL',
  'ANTHROPIC_DEFAULT_OPUS_MODEL',
  'ANTHROPIC_DEFAULT_SONNET_MODEL',
  'ANTHROPIC_MODEL',
  'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC',
  'CLAUDE_CODE_MAX_CONTEXT_TOKENS',
  'CLAUDE_CODE_MAX_OUTPUT_TOKENS',
  'CLAUDE_CODE_MAX_RETRIES',
  'CLAUDE_CODE_SUBAGENT_MODEL',
  'CLAUDE_CONFIG_DIR',
  'DISABLE_AUTOUPDATER',
  'DISABLE_ERROR_REPORTING',
  'DISABLE_TELEMETRY',
  'HOME',
  'LANG',
  'PATH',
  'PLATFORM_SESSION_TOKEN',
  'TERM',
] as const;

/**
 * A test Anthropic API key, random per run. The contract allows any printable key; this one deliberately lacks the
 * provider's `sk-ant-` shape: the key is typed into the page, so a retained Playwright trace of a failed spec holds it,
 * and the artefact scan of the run flags every `sk-ant-` value as a leaked real key.
 */
export function testApiKey(): string {
  return `e2e-api-key-${randomBytes(30).toString('base64url')}`;
}

/** A random value for one spec run (mailbox passwords, seeds). */
export const randomText = (bytes = 12) => randomBytes(bytes).toString('hex');

export const loadModelScenario = (orchestrator: Orchestrator, scenario: unknown, access: Access | null) =>
  orchestrator.call<{ double: Record<string, unknown>; access: string | null }>('POST', '/model/scenario', {
    scenario,
    access,
  });

/** The devices that hold a connection to the hub now (with `since`: one opened after that database time). */
export const connectedDevices = async (orchestrator: Orchestrator, since?: string): Promise<string[]> =>
  (
    await orchestrator.hubQuery<{ id: string }>(
      `SELECT device_id::text AS id FROM hub.device_presence${since ? ` WHERE connected_at > '${since}'` : ''}`,
    )
  ).map((row) => row.id);

/** The hub database's clock, to compare presence rows with. */
const hubNow = async (orchestrator: Orchestrator): Promise<string> =>
  (await orchestrator.hubQuery<{ now: string }>(`SELECT now()::text AS now`))[0]!.now;

/**
 * Waits until the devices that were connected before a runner restart (for a changed environment) or a hub restart
 * are connected again, so a device request or a task finds them online. Only those: an earlier spec may leave an
 * unpaired org device whose row stays `active` at the hub (refused through its membership) without a runner. With
 * `since` (a hub restart: every device reconnects) a connection counts only when it was opened after that time, so a
 * presence row left by the stopped hub instance does not end the wait early.
 */
export async function devicesOnline(orchestrator: Orchestrator, devices: readonly string[], since?: string) {
  await expect
    .poll(
      async () => {
        const connected = new Set(await connectedDevices(orchestrator, since));
        return devices.filter((device) => !connected.has(device));
      },
      { timeout: 90_000, intervals: [500] },
    )
    .toEqual([]);
}

/** Restarts the hub with `AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY` (null: unlimited); the devices reconnect. */
export async function setBudget(orchestrator: Orchestrator, usd: string | null): Promise<void> {
  const before = await connectedDevices(orchestrator);
  const since = await hubNow(orchestrator);
  await orchestrator.call<{ usd: string | null }>('POST', '/hub/budget', { usd });
  await devicesOnline(orchestrator, before, since);
}

export interface EnvCheck {
  readonly vm: string;
  readonly names: readonly string[];
  readonly model: Readonly<Record<string, string>>;
  readonly session_token: { readonly present: boolean; readonly same: boolean; readonly length: number };
  readonly anthropic_api_key_empty: boolean;
  readonly credential_findings: readonly string[];
  readonly credentials_checked: number;
}

/** The session's Claude Code environment, checked against the credential values (given, platform key, seeds). */
export const envCheck = (orchestrator: Orchestrator, session: string, values: string[] = [], seeds: string[] = []) =>
  orchestrator.call<EnvCheck>('POST', `/sessions/${uuid(session)}/env-check`, { values, login_seeds: seeds });

/** The environment of the session holds the session token in its two variables and no credential at all. */
export async function expectOnlySessionToken(
  orchestrator: Orchestrator,
  session: string,
  values: string[] = [],
  seeds: string[] = [],
): Promise<EnvCheck> {
  const env = await envCheck(orchestrator, session, values, seeds);
  expect(env.credentials_checked).toBeGreaterThanOrEqual(values.length + seeds.length);
  expect(env.credential_findings).toEqual([]);
  expect(env.session_token).toEqual({ present: true, same: true, length: expect.any(Number) });
  expect(env.session_token.length).toBeGreaterThanOrEqual(32);
  expect(env.anthropic_api_key_empty).toBe(true);
  expect(env.model.ANTHROPIC_BASE_URL).toBe('http://127.0.0.1:7700/model');
  expect(env.names.filter((name) => !(SESSION_ENV as readonly string[]).includes(name))).toEqual([]);
  return env;
}

export interface CredentialScan {
  readonly findings: readonly { readonly source: string; readonly value: number }[];
  readonly scanned: Readonly<Record<string, number>>;
  readonly values: number;
}

/**
 * 09-milestones.md M2c: no credential value in the sandbox environment, the files or the logs of the VMs, the runner
 * device and the hub side.
 */
export async function expectNoCredentialLeak(
  orchestrator: Orchestrator,
  values: string[],
  seeds: string[] = [],
): Promise<CredentialScan> {
  const scan = await orchestrator.call<CredentialScan>('POST', '/checks/credentials', { values, login_seeds: seeds });
  expect(scan.findings).toEqual([]);
  expect(scan.values).toBeGreaterThan(values.length + seeds.length - 1);
  expect(scan.scanned['runner-device']).toBeGreaterThan(0);
  expect(scan.scanned['hub:hub-db-dump']).toBeGreaterThan(0);
  expect(Object.keys(scan.scanned).some((source) => source.includes('-e2e-'))).toBe(true);
  return scan;
}

export interface LoginDoubles {
  readonly pages: string;
  readonly imaps: { readonly host: string; readonly port: number };
  readonly restarted: readonly string[];
}

export interface Mailbox {
  readonly email: string;
  readonly password: string;
}

/**
 * Starts (once) the mail and auth-page doubles, points the runners at them (restarted runners reconnect before this
 * returns) and loads the auth page's scenario.
 */
export async function loginDoubles(
  orchestrator: Orchestrator,
  options: { seed: string; junk?: boolean; fault?: string | null; account?: Mailbox },
): Promise<LoginDoubles> {
  const before = await connectedDevices(orchestrator);
  const doubles = await orchestrator.call<LoginDoubles>('POST', '/login/doubles', {
    seed: options.seed,
    junk: options.junk ?? false,
    fault: options.fault ?? null,
    ...(options.account && { account: { user: options.account.email, password: options.account.password } }),
  });
  if (doubles.restarted.length > 0) await devicesOnline(orchestrator, before);
  return doubles;
}

/** The runner's temporary login directories (must be empty after every login). */
export const loginDirs = async (orchestrator: Orchestrator, runner = 'runner-1') =>
  (await orchestrator.call<{ dirs: string[] }>('GET', `/runners/${runner}/login-dirs`)).dirs;

/** The shape of a subscription token (assembled: no source file spells a token-shaped literal). */
export const SUBSCRIPTION_TOKEN_PREFIX = ['sk', 'ant', 'oat01', ''].join('-');

/**
 * Points the runners at the usage double answering any subscription token (with the oauth beta) with `document`: a
 * spec that runs a subscription session without asserting quotas keeps the runner's quota poll inside the run.
 */
export const anySubscriptionQuota = (orchestrator: Orchestrator, document: unknown = {}) =>
  usageScenario(orchestrator, {
    name: 'any-subscription',
    document,
    token_prefix: SUBSCRIPTION_TOKEN_PREFIX,
    min_requests: 0,
  });

/** The subscription usage double's scenario (quota windows the runner polls); restarted runners reconnect first. */
export async function usageScenario(orchestrator: Orchestrator, scenario: Record<string, unknown>): Promise<void> {
  const before = await connectedDevices(orchestrator);
  const answer = await orchestrator.call<{ url: string; restarted: string[] }>('POST', '/quota/usage', scenario);
  if (answer.restarted.length > 0) await devicesOnline(orchestrator, before);
}

/** The settings screen through the header link (a reload would ask for the passkey again). */
export async function openSettings(page: Page): Promise<void> {
  await page.getByTestId('shell.settings').click();
  await expect(page.getByTestId('credentials.section')).toBeVisible({ timeout: 30_000 });
}

/** Saves the model access preference (and model: null = the default of the access kind) in the settings. */
export async function chooseModelAccess(page: Page, access: Preference, model: string | null = null): Promise<void> {
  await openSettings(page);
  await page.getByTestId(`model.access.${access}`).check();
  await page.getByTestId('model.model').selectOption(model ?? '');
  await page.getByTestId('model.save').click();
  await expect(page.getByTestId('model.saved')).toBeVisible();
}

/** Stores the Anthropic API key in the settings (encrypted in the page under the user's credential key). */
export async function storeApiKey(page: Page, key: string): Promise<void> {
  await openSettings(page);
  await page.getByTestId('api-key.input').fill(key);
  await page.getByTestId('api-key.save').click();
  await expect(page.getByTestId('api-key.saved')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('credentials.item.anthropic_api_key')).toBeVisible();
}

/** Stores the mailbox of the subscription address (the mail double's IMAPS endpoint). */
export async function storeMailbox(page: Page, doubles: LoginDoubles, mailbox: Mailbox): Promise<void> {
  await openSettings(page);
  await page.getByTestId('mailbox.imap_host').fill(doubles.imaps.host);
  await page.getByTestId('mailbox.imap_port').fill(String(doubles.imaps.port));
  await page.getByTestId('mailbox.imap_user').fill(mailbox.email);
  await page.getByTestId('mailbox.imap_password').fill(mailbox.password);
  await page.getByTestId('mailbox.email').fill(mailbox.email);
  await page.getByTestId('mailbox.save').click();
  await expect(page.getByTestId('mailbox.saved')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('credentials.item.mailbox')).toBeVisible();
}

/**
 * What the person does with the authorize link of the relay in their own browser (05-runner.md section 11.2 step 3):
 * opens it, signs in at the auth page with the magic link from their mailbox, authorizes and copies the code shown.
 * A separate browser context that trusts the auth page's test certificate.
 */
export async function authorizeAsPerson(
  browser: Browser,
  orchestrator: Orchestrator,
  url: string,
  mailbox: Mailbox,
  folder: 'INBOX' | 'Junk',
): Promise<string> {
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    const tab = await context.newPage();
    await tab.goto(url);
    await expect(tab.locator('body')).toHaveAttribute('data-page', 'email');
    await tab.locator('#email').fill(mailbox.email);
    await tab.locator('#continue').click();
    await expect(tab.locator('body')).toHaveAttribute('data-page', 'check-email');
    let link: string | null = null;
    await expect
      .poll(
        async () =>
          (link = (
            await orchestrator.call<{ link: string | null }>('POST', '/login/magic-link', {
              user: mailbox.email,
              password: mailbox.password,
              folder,
            })
          ).link),
        { timeout: 30_000 },
      )
      .not.toBeNull();
    await tab.goto(link!);
    await expect(tab.locator('body')).toHaveAttribute('data-page', 'authorize', { timeout: 30_000 });
    await tab.locator('#authorize').click();
    await expect(tab.locator('body')).toHaveAttribute('data-page', 'code', { timeout: 30_000 });
    return (await tab.locator('#authorization-code').textContent())!.trim();
  } finally {
    await context.close();
  }
}

/** Starts `claude.login` in the settings with the given mode on the preselected device. */
export async function startLogin(page: Page, mode: 'manual' | 'automatic'): Promise<void> {
  await openSettings(page);
  await page.getByTestId('claude.connect').click();
  await expect(page.getByTestId('claude.devices.select')).toBeVisible({ timeout: 30_000 });
  await page.getByTestId(`claude.mode.${mode}`).check();
  await page.getByTestId('claude.start').click();
}

/** The relay part in the page: the authorize link, the person's code pasted, the device's `done`. */
export async function finishRelay(
  page: Page,
  browser: Browser,
  orchestrator: Orchestrator,
  mailbox: Mailbox,
  folder: 'INBOX' | 'Junk',
): Promise<void> {
  const open = page.getByTestId('claude.authorize.open');
  await expect(open).toBeVisible({ timeout: 60_000 });
  const url = (await open.getAttribute('href'))!;
  expect(url).toMatch(/^https:\/\/127\.0\.0\.1:\d+\/oauth\/authorize\?/);
  const code = await authorizeAsPerson(browser, orchestrator, url, mailbox, folder);
  await page.getByTestId('claude.code').fill(code);
  await page.getByTestId('claude.code.send').click();
  await expect(page.getByTestId('claude.connected')).toBeVisible({ timeout: 60_000 });
}

/**
 * Connects a Claude subscription through the setup-token relay with the login-CLI double (05-runner.md section
 * 11.2); the device stores the token it receives (the first token the auth page issues after `seed`).
 */
export async function connectSubscription(
  page: Page,
  browser: Browser,
  orchestrator: Orchestrator,
  seed: string,
): Promise<Mailbox> {
  const mailbox = { email: `relay-${seed}@example.test`, password: randomText() };
  await loginDoubles(orchestrator, { seed, account: mailbox });
  await startLogin(page, 'manual');
  await finishRelay(page, browser, orchestrator, mailbox, 'INBOX');
  await expect(page.getByTestId('credentials.item.claude_subscription')).toBeVisible();
  return mailbox;
}

export interface UsageRow {
  readonly requests: number;
  readonly input_tokens: number;
  readonly output_tokens: number;
  readonly cache_read_tokens: number;
  readonly cache_write_tokens: number;
  readonly cost_micro_usd: number;
  readonly cost_basis: string;
  readonly context_used_tokens: number | null;
  readonly context_window_tokens: number | null;
  readonly last_provider: string | null;
  readonly model_access: string | null;
  readonly model_id: string | null;
}

/** The hub's usage counters of the task's main session (`hub.usage_counter`) with the session's access and model. */
export async function mainUsage(orchestrator: Orchestrator, task: string): Promise<UsageRow | undefined> {
  const [row] = await orchestrator.hubQuery<UsageRow>(
    `SELECT u.requests, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens,
            u.cost_micro_usd, u.cost_basis, u.context_used_tokens, u.context_window_tokens, u.last_provider,
            s.model_access, s.model_id
       FROM hub.session s JOIN hub.usage_counter u ON u.session_id = s.id
      WHERE s.task_id = '${uuid(task)}' AND s.depth = 0`,
  );
  return row;
}

/** The failure reason of the task's main session at the hub (null while it did not fail). */
export async function failureReason(orchestrator: Orchestrator, task: string): Promise<string | null> {
  const [row] = await orchestrator.hubQuery<{ state: string; state_reason: string | null }>(
    `SELECT state, state_reason FROM hub.session WHERE task_id = '${uuid(task)}' AND depth = 0`,
  );
  return row?.state === 'failed' ? row.state_reason : null;
}

interface Prices {
  readonly input: number;
  readonly output: number;
  readonly cache_read: number;
  readonly cache_write: number;
}

/** The model catalog of the run (hub `AGENT_HUB_MODEL_CATALOG`, the runners' packaged copy is the same example). */
const CATALOG = JSON.parse(
  readFileSync(new URL('../../../contracts/model-catalog.example.json', import.meta.url), 'utf8'),
) as {
  readonly models: Readonly<Record<string, { readonly context_window: number; readonly price_per_mtok: Prices }>>;
};

/** The tokens every reply of the SCN-05 scenarios reports (`usage` of scenarios/e2e-{16,27,36}-*.json). */
export const REPLY_USAGE = { input: 52_000, cache_read: 10_000, cache_write: 2_000, output: 800 } as const;
/** The context the gateway reports for any of those requests: input + cache read + cache write. */
export const REPLY_CONTEXT = REPLY_USAGE.input + REPLY_USAGE.cache_read + REPLY_USAGE.cache_write;
/** The fixed OpenRouter cost of every platform reply (`cost` of the platform scenarios), in micro-USD. */
export const PLATFORM_REPLY_MICRO_USD = 2100;

interface JournalRecord {
  readonly path: string;
  readonly status: number;
  readonly step?: string | null;
  readonly body: unknown;
}

/** The models of the requests the model double answered with a step of the scenario `name` (its journal). */
export async function answeredModels(orchestrator: Orchestrator, name: string): Promise<string[]> {
  const { body } = await orchestrator.doubleRequest('model', 'GET', '/_double/journal');
  const records = (Array.isArray(body) ? body : []) as JournalRecord[];
  return records
    .filter((record) => record.path === '/v1/messages' && record.status === 200 && record.step?.startsWith(`${name}-`))
    .map((record) => {
      const request = record.body as { model?: unknown } | null;
      return typeof request?.model === 'string' ? request.model : 'unknown';
    });
}

/** The list price (= subscription equivalent) of the answered requests in micro-USD (05-runner.md section 10.4). */
export function listPriceMicroUsd(models: readonly string[]): number {
  let total = 0;
  for (const model of models) {
    const price = CATALOG.models[model]?.price_per_mtok;
    if (price === undefined) throw new Error(`model ${model} is not in the catalog`);
    total += REPLY_USAGE.input * price.input + REPLY_USAGE.output * price.output;
    total += REPLY_USAGE.cache_read * price.cache_read + REPLY_USAGE.cache_write * price.cache_write;
  }
  return Math.round(total);
}

const BASIS_LABEL = {
  provider: 'provider',
  list_price: 'list price',
  subscription_equivalent: 'subscription equivalent',
} as const;
export type CostBasis = keyof typeof BASIS_LABEL;

/**
 * Usage of a task with one main session, 05-runner.md section 10.4 / 06-web.md section 5.2: the hub's counters hold
 * the tokens of every answered request, the cost (`expected` micro-USD from the answered requests) with its basis and
 * the context of the latest request; the task view's usage strip shows exactly these numbers. Returns the counters.
 */
export async function expectUsage(
  page: Page,
  orchestrator: Orchestrator,
  task: string,
  options: { readonly basis: CostBasis; readonly requests: number; readonly microUsd: number; readonly model: string },
): Promise<UsageRow> {
  const tokens = options.requests * REPLY_USAGE.input;
  await expect
    .poll(async () => (await mainUsage(orchestrator, task))?.input_tokens ?? 0, { timeout: 60_000 })
    .toBe(tokens);
  const row = (await mainUsage(orchestrator, task))!;
  expect(row).toMatchObject({
    input_tokens: tokens,
    output_tokens: options.requests * REPLY_USAGE.output,
    cache_read_tokens: options.requests * REPLY_USAGE.cache_read,
    cache_write_tokens: options.requests * REPLY_USAGE.cache_write,
    cost_micro_usd: options.microUsd,
    cost_basis: options.basis,
    context_used_tokens: REPLY_CONTEXT,
    context_window_tokens: CATALOG.models[options.model]!.context_window,
  });
  const cache = row.cache_read_tokens + row.cache_write_tokens;
  await expect(page.getByTestId('usage.tokens')).toHaveText(
    `${formatNumber(row.input_tokens, 'en')} in, ${formatNumber(row.output_tokens, 'en')} out, ${formatNumber(cache, 'en')} cache`,
    { timeout: 30_000 },
  );
  const cost = page.getByTestId('usage.cost');
  await expect(cost).toHaveAttribute('data-basis', options.basis);
  await expect(cost).toHaveText(`${formatMoney(row.cost_micro_usd / 1_000_000, 'en')} (${BASIS_LABEL[options.basis]})`);
  await expect(page.getByTestId('usage.context')).toHaveText(
    formatPercent(REPLY_CONTEXT / row.context_window_tokens!, 'en'),
  );
  return row;
}
