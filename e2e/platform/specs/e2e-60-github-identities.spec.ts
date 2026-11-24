// E2E-60 (07-testing.md section 2.1; ADR-078; KOR-47): GitHub identities. One organization of an owner and a member
// on the platform device, the GitHub double with five accounts (classic tokens):
// - Migration of an existing connection and a further account: the owner's first account is stored through the
//   endpoints of before (settings "Use an existing token", `PUT /v1/device/credentials/{user}/github`) and is the
//   primary identity - also for `GET /v1/me/credentials/github`; a second own account is added as a further identity.
// - A company account of the organization, connected by the admin (`github.connect` with `org_id`; the admin's
//   client seals the body for the organization's devices), later the workgroup's default.
// - The owner's task lists its identities (`github.list_identities`), checks out and branches with the primary
//   account, and two forged commits - a free-text author object and a `Co-authored-by` line in the message - are
//   refused `params_invalid` before any call to GitHub.
// - The member's prompt in the owner's task (another human instructs): no default, no own account of the member, so
//   the operation is held and the connection card appears in the chat (E-05) - the owner's personal accounts are
//   never used; once the workgroup's default names the company account the held operation goes on with it.
// - The owner's next prompt: a commit with author (second account) != committer (primary account) and both humans as
//   co-authors (their own accounts' `Co-authored-by` trailers; the double's stored commit has exactly these lines and
//   its ID is the git ID of that object), a draft pull request as the explicitly chosen second account (operation
//   parameter `github_identity`: the double has it authored by that account's token), a comment with the workgroup
//   default and a subagent with another identity (`subagent.request` `github_identity`) whose comment is made as that
//   identity.
// - The operator's account (ADR-078 section 7): the runner restarted with `OPERATOR_GITHUB_TOKEN_FILE` and
//   `OPERATOR_GITHUB_ORG` = this organization; the admin's company connect answers with the operator's token; a
//   member without an own account and without a default acts with it (step 7), without being listed anywhere.
// The hub stores `github_identity` and `triggered_by` on the operation rows and in their audit, the Operations tab
// says "as <login>, triggered by <name>"; no token, login or content reaches the hub in plaintext. T1 only: the
// operator's account changes the settings of the run's platform runner (restored at the end).
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { expectNoPlaintext } from '../fixtures/guest.ts';
import {
  connectGitHubToken,
  expectGitHubOk,
  GITHUB_SITE,
  githubJournal,
  githubRepo,
  githubRoutes,
  openGitHubSettings,
  repository,
  seedGitHub,
} from '../fixtures/github.ts';
import { closingChecks, inviteMember, joinAsNewUser, navigate, personOf } from '../fixtures/identity.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  expectAnswerInChat,
  home,
  mainSessions,
  platformDevice,
  poolReady,
  sendPrompt,
  taskBodies,
  uuid,
} from '../fixtures/platform.ts';

const scenario = JSON.parse(
  readFileSync(new URL('../scenarios/e2e-60-github-identities.json', import.meta.url), 'utf8'),
) as Record<string, unknown>;
const REPO = 'kz53-org/app';
const BRANCH = 'e2e53/notes';
/** The five GitHub accounts of the double (classic tokens with `repo`). */
const ACCOUNTS = {
  adaMain: { login: 'kz53-ada-main-r7c2', id: 5301 },
  adaSecond: { login: 'kz53-ada-second-n4x8', id: 5302 },
  bob: { login: 'kz53-bob-own-k2m6', id: 5303 },
  company: { login: 'kz53-company-d9w3', id: 5304 },
  operator: { login: 'kz53-operator-s5j1', id: 5305 },
} as const;
type Account = keyof typeof ACCOUNTS;
/** Not GitHub-shaped on purpose: tokens of the double only, never a pattern a secret scanner would report. */
const tokenOf = (account: Account): string => ['kz53', account.toLowerCase(), 'tok', 'f3g7h1j5'].join('-');
const noreply = (account: Account): string =>
  `${ACCOUNTS[account].id}+${ACCOUNTS[account].login}@users.noreply.github.com`;
const ADA = 'Ada kz53-name-ada-b8n2';
const BOB = 'Bob kz53-name-bob-x6c4';
const ADA_PROMPT = 'E2E53-ADA kz53-prompt-ada-q2w6: prepare notes for kz53-org/app';
const BOB_PROMPT = 'E2E53-BOB kz53-prompt-bob-u7i3: file an issue so that the notes get a review';
const COMMIT_PROMPT = 'E2E53-COMMIT kz53-prompt-commit-e5r9: commit the notes, push them and open the pull request';
const OPERATOR_PROMPT = 'E2E53-OPERATOR kz53-prompt-op-o1p4: file the release checklist as an issue';
const CANARIES = [
  ...Object.values(ACCOUNTS).map((account) => account.login),
  ...(Object.keys(ACCOUNTS) as Account[]).map(tokenOf),
  'kz53-name-ada-b8n2',
  'kz53-name-bob-x6c4',
  'kz53-prompt-ada-q2w6',
  'kz53-prompt-bob-u7i3',
  'kz53-prompt-commit-e5r9',
  'kz53-prompt-op-o1p4',
  'kz53-file-h4v8c2',
  'kz53-issue-bob-t5n1',
  'kz53-issue-op-y3h8',
  'kz53-title-r8d4',
  'kz53-comment-main-w2q9',
  'kz53-comment-child-j7k3',
  'kz53-child-v5b1',
  'kz53-answer-ada-p3w7',
  'kz53-answer-bob-m6x2',
  'kz53-answer-commit-c9f6',
  'kz53-answer-op-g1r5',
];
const CHECKOUT_AND_BRANCH = [
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/commits/{ref}',
  'GET /repos/{owner}/{repo}/tarball/{ref}',
  'GET /codeload/{owner}/{repo}/legacy.tar.gz/{ref}',
  'GET /repos/{owner}/{repo}',
  'GET /repos/{owner}/{repo}/git/ref/{ref}',
  'POST /repos/{owner}/{repo}/git/refs',
];
const CREATE_COMMIT = 'POST /repos/{owner}/{repo}/git/commits';

/** Every account with its classic token, and the repository (no App installation: personal tokens reach it). */
function world() {
  return {
    users: (Object.keys(ACCOUNTS) as Account[]).map((account) => ({
      login: ACCOUNTS[account].login,
      id: ACCOUNTS[account].id,
      name: null,
      email: null,
      tokens: [{ token: tokenOf(account), kind: 'classic', scopes: ['repo', 'read:org'] }],
    })),
    repositories: [repository(REPO)],
  };
}

interface IdentityMeta {
  readonly id: string;
  readonly owner_id: string;
  readonly owner_kind: 'user' | 'org';
  readonly primary: boolean;
}

/** `GET /v1/me/github-identities` of the page's user: identity metadata and the defaults. */
async function identities(user: { request: (method: string, target: string) => Promise<unknown> }) {
  return (await user.request('GET', '/v1/me/github-identities')) as {
    identities: IdentityMeta[];
    defaults: { scope: string; scope_id: string; identity_id: string }[];
  };
}

interface OperationRow {
  readonly id: string;
  readonly session_id: string;
  readonly type: string;
  readonly state: string;
  readonly error_code: string | null;
  readonly github_identity: string | null;
  readonly triggered_by: string | null;
}

/** The hub's operation rows of a task (oldest first) with the identity and the human of each. */
const operations = (orchestrator: Orchestrator, task: string) =>
  orchestrator.hubQuery<OperationRow>(
    `SELECT id, session_id, type, state, error_code, github_identity::text, triggered_by::text FROM hub.operation
      WHERE task_id = '${uuid(task)}' ORDER BY created_at, id`,
  );

/** "Use an existing token" in a connect panel (`<panel>.token`): the device choice, the token, start, done. */
async function connectInPanel(page: Page, panel: string, token: string | null): Promise<void> {
  await page
    .getByTestId(panel)
    .getByTestId(token === null ? `${panel}.device-flow` : `${panel}.token`)
    .click();
  if (token !== null) {
    const input = page.getByTestId('github.token.input');
    await expect(input).toBeVisible({ timeout: 60_000 });
    await input.fill(token);
  }
  await expect(page.getByTestId('github.start')).toBeEnabled({ timeout: 60_000 });
  await page.getByTestId('github.start').click();
}

/** The workgroup's default GitHub identity in its settings (`null`: none). */
async function setWorkgroupDefault(page: Page, org: string, wg: string, identity: string | null): Promise<void> {
  await navigate(page, `/o/${org}/w/${wg}/settings`);
  const select = page.getByTestId('wg-settings.github.select');
  await expect(select).toBeEnabled({ timeout: 30_000 });
  await select.selectOption(identity ?? '');
  await expect(select).toBeEnabled({ timeout: 30_000 });
  await expect(select).toHaveValue(identity ?? '');
}

/** The git object ID of the commit the double stored from a `POST …/git/commits` body (`objects.Commit.payload`). */
function commitId(body: CommitBody): string {
  const person = (who: Person) => `${who.name} <${who.email}> ${Date.parse(who.date) / 1000} +0000`;
  const lines = [`tree ${body.tree}`, ...body.parents.map((parent) => `parent ${parent}`)];
  lines.push(`author ${person(body.author)}`, `committer ${person(body.committer)}`);
  const payload = Buffer.from(`${lines.join('\n')}\n\n${body.message}`, 'utf8');
  return createHash('sha1')
    .update(Buffer.concat([Buffer.from(`commit ${payload.length}\0`), payload]))
    .digest('hex');
}

interface Person {
  readonly name: string;
  readonly email: string;
  readonly date: string;
}

interface CommitBody {
  readonly message: string;
  readonly tree: string;
  readonly parents: readonly string[];
  readonly author: Person;
  readonly committer: Person;
  readonly signature?: string;
}

/** The whole journal of the model double as text (what the sessions sent to the model). */
async function modelJournalText(orchestrator: Orchestrator): Promise<string> {
  return JSON.stringify((await orchestrator.doubleRequest('model', 'GET', '/_double/journal')).body);
}

interface ModelRequest {
  readonly path: string;
  readonly body: { readonly messages?: readonly { role: string; content: unknown }[] } | null;
}

/** The platform answers (JSON objects of `tool_result` blocks) of the newest model request that carries any. */
async function toolResults(orchestrator: Orchestrator): Promise<Record<string, unknown>[]> {
  const journal = (await orchestrator.doubleRequest('model', 'GET', '/_double/journal')).body as ModelRequest[];
  const found: Record<string, unknown>[][] = [];
  for (const entry of journal) {
    if (!entry.path.startsWith('/v1/messages') || entry.path.includes('count_tokens')) continue;
    const out: Record<string, unknown>[] = [];
    for (const message of entry.body?.messages ?? []) {
      if (message.role !== 'user' || !Array.isArray(message.content)) continue;
      for (const block of message.content as { type: string; content?: unknown }[]) {
        if (block.type !== 'tool_result') continue;
        const content = block.content;
        const text =
          typeof content === 'string' ? content : (content as { text?: string }[]).map((c) => c.text ?? '').join('');
        if (text.startsWith('{')) out.push(JSON.parse(text) as Record<string, unknown>);
      }
    }
    if (out.length > 0) found.push(out);
  }
  return found.at(-1) ?? [];
}

/** Sets (or with `null` removes) the operator's GitHub account of the platform runner and waits for the device. */
async function operatorAccount(orchestrator: Orchestrator, device: string, org: string | null): Promise<void> {
  const body = org === null ? { token: null } : { token: tokenOf('operator'), org };
  await orchestrator.call('POST', '/runners/runner-1/operator-github', body);
  await platformDevice(orchestrator);
  expect(await platformDevice(orchestrator)).toBe(device);
}

test.describe.configure({ mode: 'serial' });

test('@E2E-60 @R-9.1 @R-9.4 @needs-vm several GitHub identities, company and operator accounts: each operation acts as the chosen or usable account and never as another person', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.skip(e2e.topology !== 't1', 'E2E-60 changes the platform runner of the run (T1)');
  test.setTimeout(1_800_000);
  await seedGitHub(orchestrator, world());
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator);
  const ada = await personOf(page, await authenticator('platform'), orchestrator, ADA);
  const org = ada.onboarded.org;
  const [orgRow] = await orchestrator.hubQuery<{ default_wg: string }>(
    `SELECT default_wg FROM hub.org WHERE id = '${uuid(org)}'`,
  );
  const wg = orgRow!.default_wg;

  // -- Migration of an existing connection, then a further own account ----------------------------------------------
  const ids =
    await test.step('the first account is stored as before and is primary; a second one is added', async () => {
      await connectGitHubToken(page, tokenOf('adaMain'), ACCOUNTS.adaMain.login);
      const accounts = page.getByTestId('github.accounts.item');
      await expect(accounts).toHaveCount(1, { timeout: 30_000 });
      await expect(accounts.getByTestId('github.accounts.primary')).toBeVisible();
      await page.getByTestId('github.accounts.add').click();
      await connectInPanel(page, 'github.accounts.add-panel', tokenOf('adaSecond'));
      await expect(accounts).toHaveCount(2, { timeout: 60_000 });
      const second = accounts.filter({ hasText: ACCOUNTS.adaSecond.login });
      await expect(second.getByTestId('github.accounts.primary')).toHaveCount(0);
      await expect(second.getByTestId('github.accounts.make-primary')).toBeVisible();
      await expect(
        accounts.filter({ hasText: ACCOUNTS.adaMain.login }).getByTestId('github.accounts.primary'),
      ).toBeVisible();
      // the page still names the primary account as "the" connection
      await expect(page.getByTestId('github.connected')).toHaveText(`Connected as ${ACCOUNTS.adaMain.login}`);

      const listed = await identities(ada.user);
      const own = listed.identities.filter((item) => item.owner_kind === 'user' && item.owner_id === ada.user.userId);
      expect(own.map((item) => item.primary).sort()).toEqual([false, true]);
      const main = own.find((item) => item.primary)!.id;
      const secondId = own.find((item) => !item.primary)!.id;
      await expect(accounts.filter({ hasText: ACCOUNTS.adaMain.login })).toHaveAttribute('data-identity', main);
      // The endpoints of before address the primary record: an old browser or runner keeps its account.
      const legacy = (await ada.user.request('GET', '/v1/me/credentials/github')) as {
        record: { header: { id: string } };
      };
      expect(legacy.record.header.id).toBe(main);
      const rows = await orchestrator.hubQuery<{ id: string; is_primary: boolean; owner_kind: string; actor: string }>(
        `SELECT c.id::text, c.is_primary, c.owner_kind,
              (SELECT a.actor_kind FROM hub.audit a WHERE a.action = 'credential.put' AND a.target_id = c.id::text
                ORDER BY a.seq LIMIT 1) AS actor
         FROM hub.credential c WHERE c.owner_id = '${uuid(ada.user.userId)}' AND c.kind = 'github' ORDER BY c.created_at`,
      );
      expect(rows).toEqual([
        { id: main, is_primary: true, owner_kind: 'user', actor: 'device' },
        { id: secondId, is_primary: false, owner_kind: 'user', actor: 'device' },
      ]);
      return { main, second: secondId };
    });

  // -- The member and the company account ---------------------------------------------------------------------------
  const bob = await joinAsNewUser(browser, e2e, await inviteMember(page, org), BOB);
  const company = await test.step('the admin connects a company account of the organization', async () => {
    await navigate(page, `/o/${org}/github`);
    await expect(page.getByTestId('company-github.empty')).toBeVisible({ timeout: 30_000 });
    await page.getByTestId('company-github.add').click();
    await connectInPanel(page, 'company-github.add-panel', tokenOf('company'));
    const item = page.getByTestId('company-github.item');
    await expect(item).toHaveCount(1, { timeout: 60_000 });
    await expect(item.getByTestId('company-github.label')).toContainText(ACCOUNTS.company.login);
    const id = (await item.getAttribute('data-identity'))!;
    const [row] = await orchestrator.hubQuery<{ owner_id: string; owner_kind: string; is_primary: boolean }>(
      `SELECT owner_id::text, owner_kind, is_primary FROM hub.credential WHERE id = '${uuid(id)}'`,
    );
    expect(row).toEqual({ owner_id: org, owner_kind: 'org', is_primary: false });
    // the member sees the company account by metadata, without controls (the screen loads the identities anew)
    await navigate(bob.page, `/o/${org}/github`);
    await expect(bob.page.getByTestId('company-github.item')).toHaveCount(1, { timeout: 30_000 });
    await expect(bob.page.getByTestId('company-github.add')).toHaveCount(0);
    return id;
  });

  await orchestrator.loadScenario('model', {
    ...scenario,
    variables: { ada: ada.user.userId, bob: bob.user.userId, ada_main: ids.main, ada_second: ids.second },
  });
  const connectCalls = (await githubRoutes(orchestrator)).length;

  // -- The owner's task: identities, checkout and branch with the primary account, forged commits refused ----------
  await home(page);
  const task = await sendPrompt(page, ADA_PROMPT);
  const taskPath = `/o/${org}/t/${task}`;
  await expectAnswerInChat(page, 'The notes are prepared kz53-answer-ada-p3w7.');
  const [session] = await mainSessions(orchestrator, task);
  await test.step('the identities the session may use; nothing of the forged commits reaches GitHub', async () => {
    // the account check (`GET /user` with the identity's token, once per record version) comes before the first
    // pre-check of an identity; otherwise exactly checkout and branch
    const routes = await githubRoutes(orchestrator, connectCalls);
    expect(routes.filter((route) => route !== 'GET /user')).toEqual(CHECKOUT_AND_BRANCH);
    expect(routes.filter((route) => route === 'GET /user')).toHaveLength(1);
    const rows = await operations(orchestrator, task);
    expect(rows.map((row) => [row.type, row.state, row.error_code])).toEqual([
      ['github.list_identities', 'succeeded', null],
      ['github.checkout', 'succeeded', null],
      ['github.create_branch', 'succeeded', null],
      ['github.commit', 'rejected', 'params_invalid'],
      ['github.commit', 'rejected', 'params_invalid'],
    ]);
    for (const row of rows.slice(1, 3))
      expect([row.github_identity, row.triggered_by]).toEqual([ids.main, ada.user.userId]);
    const results = await taskBodies(page, orchestrator, ada.onboarded, task, 'operation.result');
    const listed = results.find((body) => body.operation_id === rows[0]!.id) as {
      result: {
        acting_user: string;
        current: string | null;
        identities: { id: string; login: string; owner_kind: string; primary: boolean; source: string }[];
        co_authors: { user_id: string; login: string }[];
      };
    };
    expect(listed.result.acting_user).toBe(ada.user.userId);
    expect(listed.result.current).toBe(ids.main);
    expect(
      listed.result.identities
        .map((item) => [item.id, item.login, item.owner_kind, item.primary, item.source])
        .sort((a, b) => String(a[1]).localeCompare(String(b[1]))),
    ).toEqual(
      [
        [ids.main, ACCOUNTS.adaMain.login, 'user', true, 'own'],
        [ids.second, ACCOUNTS.adaSecond.login, 'user', false, 'own'],
        [company, ACCOUNTS.company.login, 'org', false, 'task_org'],
      ].sort((a, b) => String(a[1]).localeCompare(String(b[1]))),
    );
    expect(listed.result.co_authors).toEqual([{ user_id: ada.user.userId, login: ACCOUNTS.adaMain.login }]);
    // what the session got for the forged commits: refused by the formal checks, the trailer named by its pointer
    const refused = (await toolResults(orchestrator)).filter((result) => result.status === 'rejected') as {
      error: { code: string; pointers?: string[] };
    }[];
    expect(refused.map((result) => result.error.code)).toEqual(['params_invalid', 'params_invalid']);
    expect(refused[1]!.error.pointers).toEqual(['/params/message']);
  });

  // -- Another human instructs: held for a connection, never the owner's account; then the workgroup default -------
  await test.step("the member's instruction waits for a connection, then acts as the workgroup's company account", async () => {
    const before = (await githubRoutes(orchestrator)).length;
    await navigate(bob.page, taskPath);
    await bob.page.getByTestId('chat.composer.prompt').fill(BOB_PROMPT);
    await bob.page.getByTestId('chat.composer.send').click();
    // the card asks the member (the acting user) to connect; the owner only sees whom it waits for
    const card = bob.page.getByTestId('waiting.card');
    await expect(card).toHaveAttribute('data-status', 'open', { timeout: 90_000 });
    await expect(card).toHaveAttribute('data-reason', 'github_connection');
    await expect(card).toHaveAttribute('data-owner', 'true');
    await expect(card.getByTestId('waiting.operation')).toHaveAttribute('data-type', 'github.create_issue');
    await expect(card.getByTestId('waiting.repo')).toHaveText(REPO);
    const other = page.getByTestId('waiting.card');
    await expect(other).toHaveAttribute('data-owner', 'false', { timeout: 60_000 });
    await expect(other.getByTestId('waiting.other')).toHaveText(`Waiting for ${BOB} to connect GitHub.`);
    const waiting = (await taskBodies(page, orchestrator, ada.onboarded, task, 'session.status')).filter(
      (body) => body.state === 'waiting',
    );
    expect(waiting).toEqual([
      expect.objectContaining({ waiting_for: 'github_connection', for_user: bob.user.userId, repo: REPO }),
    ]);
    // held on the device: unknown to the hub, no call to GitHub with any token
    expect((await operations(orchestrator, task)).filter((row) => row.type === 'github.create_issue')).toEqual([]);
    expect(await githubRoutes(orchestrator, before)).toEqual([]);

    await setWorkgroupDefault(page, org, wg, company);
    expect((await identities(ada.user)).defaults).toEqual([
      expect.objectContaining({ scope: 'wg', scope_id: wg, identity_id: company }),
    ]);
    await navigate(page, taskPath);
    await expectAnswerInChat(page, 'Issue #1 is filed kz53-answer-bob-m6x2.');
    await expect(page.getByTestId('waiting.card')).toHaveAttribute('data-status', 'done', { timeout: 60_000 });
    const issues = (await githubRepo(orchestrator, REPO)).issues;
    expect(issues.map((issue) => [issue.number, issue.title, issue.user])).toEqual([
      [1, 'Review the notes kz53-issue-bob-t5n1', ACCOUNTS.company.login],
    ]);
    const [issueOp] = (await operations(orchestrator, task)).filter((row) => row.type === 'github.create_issue');
    expect([issueOp!.state, issueOp!.github_identity, issueOp!.triggered_by]).toEqual([
      'succeeded',
      company,
      bob.user.userId,
    ]);
  });

  // -- The member's own account (co-author), then the owner's commit, pull request and subagent --------------------
  await connectGitHubToken(bob.page, tokenOf('bob'), ACCOUNTS.bob.login);
  const commitFrom = (await githubJournal(orchestrator)).length;
  await page.getByTestId('chat.composer.prompt').fill(COMMIT_PROMPT);
  await page.getByTestId('chat.composer.send').click();
  await expect(
    page
      .getByTestId('chat.output')
      .filter({ hasText: `Pull request #2 is open: ${GITHUB_SITE}/${REPO}/pull/2 kz53-answer-commit-c9f6.` }),
  ).toBeVisible({ timeout: 420_000 });

  await test.step('author != committer, two human co-authors: exactly these in the commit GitHub stored', async () => {
    const commits = (await githubJournal(orchestrator))
      .slice(commitFrom)
      .filter((call) => call.route === CREATE_COMMIT);
    expect(commits.map((call) => call.status)).toEqual([201]);
    const body = commits[0]!.body as CommitBody;
    expect([body.author.name, body.author.email]).toEqual([ACCOUNTS.adaSecond.login, noreply('adaSecond')]);
    expect([body.committer.name, body.committer.email]).toEqual([ACCOUNTS.adaMain.login, noreply('adaMain')]);
    expect(body.message).toBe(
      'Add the notes.\n\n' +
        `Co-authored-by: ${ACCOUNTS.adaMain.login} <${noreply('adaMain')}>\n` +
        `Co-authored-by: ${ACCOUNTS.bob.login} <${noreply('bob')}>\n`,
    );
    expect(body.signature).toBeUndefined();
    const repo = await githubRepo(orchestrator, REPO);
    const head = repo.refs['heads/e2e53/notes'] ?? repo.refs['refs/heads/e2e53/notes'];
    expect(head).toBe(commitId(body));
  });

  await test.step('the pull request as the chosen account, the comments as the default and as the subagent identity', async () => {
    const repo = await githubRepo(orchestrator, REPO);
    expect(repo.pulls.map((pull) => [pull.number, pull.draft, pull.head, pull.user])).toEqual([
      [2, true, BRANCH, ACCOUNTS.adaSecond.login],
    ]);
    expect(repo.comments.map((comment) => [comment.number, comment.body, comment.user])).toEqual([
      [2, 'Ready for review kz53-comment-main-w2q9', ACCOUNTS.company.login],
      [2, 'Reviewed kz53-comment-child-j7k3', ACCOUNTS.adaMain.login],
    ]);
    const sessions = await orchestrator.hubQuery<{ id: string; depth: number; github_identity: string | null }>(
      `SELECT id::text, depth, github_identity::text FROM hub.session WHERE task_id = '${uuid(task)}' ORDER BY depth`,
    );
    expect(sessions.map((row) => [row.depth, row.github_identity])).toEqual([
      [0, null],
      [1, ids.main],
    ]);
    const child = sessions[1]!.id;
    const rows = (await operations(orchestrator, task)).slice(6);
    expect(
      rows.map((row) => [
        row.type,
        row.state,
        row.session_id === child ? 'child' : row.session_id === session!.id ? 'main' : row.session_id,
        row.github_identity,
        row.triggered_by,
      ]),
    ).toEqual([
      ['github.commit', 'succeeded', 'main', company, ada.user.userId],
      ['github.push', 'succeeded', 'main', company, ada.user.userId],
      ['github.create_pull_request', 'succeeded', 'main', ids.second, ada.user.userId],
      ['github.comment', 'succeeded', 'main', company, ada.user.userId],
      // the request names the child's identity (`subagent.request` `github_identity`)
      ['subagent.request', 'succeeded', 'main', ids.main, ada.user.userId],
      ['github.comment', 'succeeded', 'child', ids.main, ada.user.userId],
    ]);
    // the audit of the pull request names the identity and the human as well
    const [audit] = await orchestrator.hubQuery<{ detail: Record<string, unknown> }>(
      `SELECT detail FROM hub.audit WHERE action = 'operation.succeeded' AND target_id = '${uuid(rows[2]!.id)}'`,
    );
    expect(audit!.detail).toMatchObject({ github_identity: ids.second, triggered_by: ada.user.userId });

    // The Operations tab: "as <login>, triggered by <name>".
    await page.getByTestId('task.tabs.operations').click();
    const pr = page.locator(`[data-testid="operations.item"][data-operation="${rows[2]!.id}"]`);
    await expect(pr.getByTestId('operations.attribution')).toContainText(ACCOUNTS.adaSecond.login, { timeout: 60_000 });
    await expect(pr.getByTestId('operations.triggered-by')).toHaveText(`triggered by ${ADA}`);
    const issue = page.locator(
      `[data-testid="operations.item"][data-operation="${(await operations(orchestrator, task))[5]!.id}"]`,
    );
    await expect(issue.getByTestId('identities.name')).toHaveAttribute('data-kind', 'company');
    await expect(issue.getByTestId('operations.triggered-by')).toHaveText(`triggered by ${BOB}`);
  });

  // -- The operator's account for members of its organization --------------------------------------------------------
  try {
    await test.step("a member without an own account and without a default acts with the operator's account", async () => {
      await setWorkgroupDefault(page, org, wg, null);
      expect((await identities(ada.user)).defaults).toEqual([]);
      await operatorAccount(orchestrator, device, org);
      await navigate(page, `/o/${org}/github`);
      await expect(page.getByTestId('company-github.item')).toHaveCount(1, { timeout: 30_000 });
      await page.getByTestId('company-github.add').click();
      await connectInPanel(page, 'company-github.add-panel', null);
      await expect(page.getByTestId('company-github.item')).toHaveCount(2, { timeout: 90_000 });
      const operatorId = (await identities(ada.user)).identities.find(
        (item) => item.owner_kind === 'org' && item.id !== company,
      )!.id;

      // the member gives up its own account; nobody lists it anywhere
      await openGitHubSettings(bob.page);
      await bob.page.getByTestId('github.disconnect').click();
      await bob.page.getByTestId('github.disconnect.confirm').click();
      await expect(bob.page.getByTestId('github.disconnected')).toBeVisible({ timeout: 30_000 });

      await home(bob.page);
      const own = await sendPrompt(bob.page, OPERATOR_PROMPT);
      await expectAnswerInChat(bob.page, 'Issue #3 is filed kz53-answer-op-g1r5.');
      const issues = (await githubRepo(orchestrator, REPO)).issues;
      expect(issues.map((issue) => [issue.number, issue.user])).toEqual([
        [1, ACCOUNTS.company.login],
        [3, ACCOUNTS.operator.login],
      ]);
      const rows = await operations(orchestrator, own);
      expect(rows.map((row) => [row.type, row.state, row.github_identity, row.triggered_by])).toEqual([
        ['github.create_issue', 'succeeded', operatorId, bob.user.userId],
      ]);
    });
  } finally {
    await operatorAccount(orchestrator, device, null);
  }

  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  const model = await modelJournalText(orchestrator);
  for (const account of Object.keys(ACCOUNTS) as Account[]) expect(model).not.toContain(tokenOf(account));
  await expectGitHubOk(orchestrator);
  await closingChecks(orchestrator, true);
  await expectNoPlaintext(orchestrator, CANARIES);
  await bob.context.close();
});
