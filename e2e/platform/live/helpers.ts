// Helpers of the live specs (07-testing.md sections 2.4 and 6; LVT-01): operator values, credentials deposited in
// Node, the OpenRouter cost guard and the GitHub test repository. Secrets come only from the run's environment
// (`E2E_LIVE_*`, handed over by `live/driver.py` on standard input) and stay in this Node process: credentials are
// sealed here under the test user's credential key, so the page (and its trace) only ever holds ciphertext; every
// provider call goes through the live bridge on the runner device (the run's only allowed egress).
import { mkdirSync, writeFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { newHeader, sealCredential } from '../../../web/src/core/crypto/credential';
import { unwrapKey } from '../../../web/src/core/crypto/wrap';
import { uuid7 } from '../../../web/src/core/encoding/ids';
import { bodyJson } from '../../../web/src/core/encoding/json';
import { formatTime } from '../../../web/src/core/encoding/time';

/** A required operator value; a missing one fails the test with the variable's name (never a value). */
export function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set: the operator hands it to live runs (live/driver.py)`);
  return value;
}

/** Writes the (secret-free) result of a live test to `<iteration>/live/<name>.json`. */
export function writeResult(iterationDir: string, name: string, results: Record<string, unknown>): void {
  mkdirSync(`${iterationDir}/live`, { recursive: true });
  writeFileSync(`${iterationDir}/live/${name}.json`, JSON.stringify(results, null, 1));
}

interface Wrap {
  readonly scope: string;
  readonly scope_id: string;
  readonly epoch: number;
  readonly recipient_id: string;
  readonly wrapped_by: string;
  readonly wrap: string;
}

export type CredentialKind = 'claude_subscription' | 'anthropic_api_key' | 'github';

/**
 * Stores `body` as the user's credential `kind` (version 1) like the web core's `putCredential` (04-crypto.md section
 * 4.4): sealed in Node under the newest own credential key `CK` (the self-wrap from the registration), signed by the
 * user; the page only signs the request that carries the sealed record.
 */
export async function depositCredential(
  page: Page,
  recoveryCode: string,
  kind: CredentialKind,
  body: Record<string, unknown>,
): Promise<string> {
  const user = await PageUser.open(page, recoveryCode);
  const listing = (await user.request('GET', `/v1/wraps?scope=cred&scope_id=${user.userId}`)) as { wraps: Wrap[] };
  const own = listing.wraps
    .filter((wrap) => wrap.recipient_id === user.userId && wrap.wrapped_by === user.userId)
    .sort((a, b) => b.epoch - a.epoch)[0];
  if (own === undefined) throw new Error('the test user has no credential key');
  const slot = { scope: 'cred', scope_id: user.userId, epoch: own.epoch, recipient: user.userId };
  const key = await unwrapKey(user.identity.kem, slot, own.wrap);
  const header = newHeader({
    id: uuid7(Date.now()),
    owner: user.userId,
    kind,
    version: 1,
    epoch: own.epoch,
    author: user.userId,
    ts: formatTime(Date.now()),
  });
  const record = await sealCredential(key, header, bodyJson(body), user.identity.sig.privateKey);
  await user.request('PUT', `/v1/me/credentials/${kind}`, { record, expected_version: 0 });
  return user.userId;
}

/** The live bridge's journal (method, upstream, path without query, status), in order. */
export async function bridgeJournal(
  orchestrator: Orchestrator,
): Promise<{ method: string; upstream: string; path: string; status: number }[]> {
  return (await orchestrator.call<{ entries: [] }>('GET', '/live/journal')).entries;
}

// ------------------------------------------------------------------------------------------------ cost guard

export const COST_CAP_USD = 1.0;
const GUARD_INTERVAL_MS = 10_000;

/**
 * The OpenRouter key's usage in USD (`GET /api/v1/key` through the bridge): `usage` (all time, never reset - the
 * growth during a run is the same as that of `usage_daily`, also across midnight UTC) and `usage_daily`.
 */
export async function keyUsage(base: string, key: string): Promise<{ total: number; daily: number }> {
  const response = await fetch(`${base}/openrouter/v1/key`, { headers: { Authorization: `Bearer ${key}` } });
  if (response.status !== 200) throw new Error(`OpenRouter key endpoint answered ${response.status}`);
  const body = (await response.json()) as { data?: { usage?: unknown; usage_daily?: unknown } };
  const total = body.data?.usage;
  const daily = body.data?.usage_daily;
  if (typeof total !== 'number' || typeof daily !== 'number') throw new Error('the OpenRouter key answer has no usage');
  return { total, daily };
}

/**
 * The cost cap of 07-testing.md section 6: reads the key's usage every 10 s and calls `abort` once it grew by more
 * than USD 1.00 since `start` - or once three reads in a row failed (fail closed: an unreadable usage is no proof of
 * staying below the cap). OpenRouter books with a delay, so `stop` also returns the final growth for the caller to
 * check together with the cost the hub recorded.
 */
export class CostGuard {
  private before = 0;
  private failures = 0;
  private timer: ReturnType<typeof setInterval> | undefined;
  exceeded = false;
  reason: 'cap' | 'unreadable' | null = null;

  constructor(
    private readonly base: string,
    private readonly key: string,
    private readonly abort: () => Promise<void>,
  ) {}

  async start(): Promise<number> {
    this.before = (await keyUsage(this.base, this.key)).total;
    this.timer = setInterval(() => {
      keyUsage(this.base, this.key).then(
        async (now) => {
          this.failures = 0;
          if (now.total - this.before > COST_CAP_USD) await this.trip('cap');
        },
        async () => {
          this.failures += 1;
          if (this.failures >= 3) await this.trip('unreadable');
        },
      );
    }, GUARD_INTERVAL_MS);
    return this.before;
  }

  private async trip(reason: 'cap' | 'unreadable'): Promise<void> {
    if (this.exceeded) return;
    this.exceeded = true;
    this.reason = reason;
    await this.abort();
  }

  async stop(): Promise<{ before: number; after: number; grown: number }> {
    clearInterval(this.timer);
    const after = (await keyUsage(this.base, this.key)).total;
    return { before: this.before, after, grown: Math.round((after - this.before) * 1e6) / 1e6 };
  }
}

// ------------------------------------------------------------------------------------------------ GitHub

/** The test repository and the token of the operator's GitHub login, through the live bridge. */
export class GitHub {
  constructor(
    readonly api: string,
    private readonly token: string,
    readonly repo: string,
  ) {}

  async call<T = unknown>(method: string, path: string, body?: unknown): Promise<{ status: number; body: T }> {
    const response = await fetch(`${this.api}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${this.token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: body === undefined ? null : JSON.stringify(body),
    });
    const text = await response.text();
    return { status: response.status, body: (text ? JSON.parse(text) : null) as T };
  }

  async ok<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
    const answer = await this.call<T>(method, path, body);
    if (answer.status < 200 || answer.status >= 300)
      throw new Error(`GitHub ${method} ${path.split('?')[0]}: ${answer.status}`);
    return answer.body;
  }

  /** The login, ID and OAuth scopes of the token (`GET /user`). */
  async user(): Promise<{ login: string; id: number; scopes: string[] }> {
    const response = await fetch(`${this.api}/user`, {
      headers: { Authorization: `Bearer ${this.token}`, Accept: 'application/vnd.github+json' },
    });
    if (response.status !== 200) throw new Error(`GitHub GET /user: ${response.status}`);
    const body = (await response.json()) as { login: string; id: number };
    const scopes = (response.headers.get('x-oauth-scopes') ?? '')
      .split(',')
      .map((scope) => scope.trim())
      .filter((scope) => /^[a-z0-9:_-]{1,64}$/.test(scope));
    return { login: body.login, id: body.id, scopes };
  }

  async headOf(branch: string): Promise<string | null> {
    const answer = await this.call<{ object: { sha: string } }>(
      'GET',
      `/repos/${this.repo}/git/ref/heads/${encodeURI(branch)}`,
    );
    return answer.status === 200 ? answer.body.object.sha : null;
  }

  async createBranch(branch: string, sha: string): Promise<void> {
    await this.ok('POST', `/repos/${this.repo}/git/refs`, { ref: `refs/heads/${branch}`, sha });
  }

  /** The highest issue or pull-request number of the repository (0 for none): the next one is this + 1. */
  async lastNumber(): Promise<number> {
    const items = await this.ok<{ number: number }[]>(
      'GET',
      `/repos/${this.repo}/issues?state=all&sort=created&direction=desc&per_page=1`,
    );
    const pulls = await this.ok<{ number: number }[]>(
      'GET',
      `/repos/${this.repo}/pulls?state=all&sort=created&direction=desc&per_page=1`,
    );
    return Math.max(0, ...items.map((item) => item.number), ...pulls.map((pull) => pull.number));
  }

  /**
   * Cleanup (07-testing.md section 6): closes every open pull request whose head starts with `prefix` and deletes
   * every branch that starts with it. Returns what it closed and deleted.
   */
  async cleanup(prefix: string): Promise<{ closed: number[]; deleted: string[]; errors: string[] }> {
    const closed: number[] = [];
    const deleted: string[] = [];
    const errors: string[] = [];
    // every step is tried, a failure is collected (the driver's fallback cleanup runs after the run as well)
    const step = async (what: string, work: () => Promise<void>) => {
      try {
        await work();
      } catch (error) {
        errors.push(`${what}: ${(error as Error).message}`);
      }
    };
    let pulls: { number: number; head: { ref: string } }[] = [];
    await step('list pull requests', async () => {
      pulls = await this.ok('GET', `/repos/${this.repo}/pulls?state=open&per_page=100`);
    });
    for (const pull of pulls.filter((item) => item.head.ref.startsWith(prefix)))
      await step(`close #${pull.number}`, async () => {
        await this.ok('PATCH', `/repos/${this.repo}/pulls/${pull.number}`, { state: 'closed' });
        closed.push(pull.number);
      });
    let refs: { ref: string }[] = [];
    await step('list branches', async () => {
      refs = await this.ok('GET', `/repos/${this.repo}/git/matching-refs/heads/${prefix}`);
    });
    for (const ref of refs) {
      const branch = ref.ref.replace(/^refs\/heads\//, '');
      await step(`delete ${branch}`, async () => {
        await this.ok('DELETE', `/repos/${this.repo}/git/refs/heads/${encodeURI(branch)}`);
        deleted.push(branch);
      });
    }
    return { closed, deleted, errors };
  }

  /** The repository must be private (the token can write to every repository of the test account). */
  async checkPrivate(): Promise<void> {
    const repo = await this.ok<{ private: boolean }>('GET', `/repos/${this.repo}`);
    if (repo.private !== true) throw new Error('AGENT_LIVE_GITHUB_REPO is not a private repository');
  }
}
