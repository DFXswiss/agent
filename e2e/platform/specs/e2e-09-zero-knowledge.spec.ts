// E2E-09 (07-testing.md section 2.1; 00-overview.md section 3; 04-crypto.md sections 7 and 9; ADR-003): zero knowledge
// at the hub over the full flows. The flows of E2E-01 (inviter and platform invitation), E2E-06 (first task), E2E-25
// (whiteboards on three levels, a member reading them), E2E-27 (stored credentials, a turn with the user's API key)
// and E2E-21 (GitHub operation chain) run with canaries that are random per run - an ASCII and a non-ASCII one per
// field - in prompts, answers of the model double, the task title, whiteboard entries, names of the organization,
// workgroup, members and device, credentials, operation parameters and results; invitation and recovery codes are
// canaries too. Asserted: no canary in any encoding in the full hub database dump, any hub or hub-PostgreSQL log, any
// byte that crossed the browser or the runner link in either direction (requests, responses, SSE and WebSocket frames
// recorded on the hub's side of the relays) or an organization export; the canaries are in the browser (pages of two
// users) and on the runner device (double journals, runner logs, runner store); every hub column maps onto a row of
// 04-crypto.md section 7 and holds only tokens where the map says metadata. The report goes to the iteration's
// artefacts (`e2e-09/zero-knowledge.json`, labels only). Runs on T1 and T2.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/test.ts';
import { addAuthenticator } from '../fixtures/authenticator.ts';
import { GITHUB_SITE, githubJournal } from '../fixtures/github.ts';
import {
  addCanaryValues,
  both,
  type CanaryPair,
  fill,
  firstTask,
  githubChain,
  newCanaries,
} from '../fixtures/flows.ts';
import { deviceItem } from '../fixtures/identity.ts';
import { CLAUDE_MODEL, chooseModelAccess, loadModelScenario, openSettings, storeApiKey } from '../fixtures/model.ts';
import {
  home,
  onboard,
  onboardWithCode,
  operator,
  platformDevice,
  poolReady,
  sendPrompt,
  expectAnswerInChat,
} from '../fixtures/platform.ts';
import { inviteMember, joinAsNewUser, writeEntry } from '../fixtures/team.ts';
import { navigateInApp } from '../../../web/e2e/helpers/whiteboard.ts';
import { PageUser } from '../../../web/e2e/helpers/user.ts';
import { parseCode } from '../../../web/src/core/crypto/invite';
import { parseRecoveryCode } from '../../../web/src/core/crypto/recovery';

const scenario = (name: string): unknown =>
  JSON.parse(readFileSync(new URL(`../scenarios/${name}.json`, import.meta.url), 'utf8'));
const FIRST = scenario('e2e-09-first-task');
const API_KEY = scenario('e2e-09-api-key');
const GITHUB = scenario('e2e-09-github');

const LABELS = [
  // names (00-overview.md section 3: organizations, workgroups, members, devices)
  'name-inviter',
  'name-invitee',
  'name-member',
  'name-org',
  'name-profile',
  'name-wg',
  'name-device',
  // prompts, answers, titles
  'prompt',
  'answer',
  'title',
  'side',
  'prompt-key',
  'answer-key',
  'prompt-github',
  'answer-github',
  // whiteboards
  'wb-org',
  'wb-wg',
  'wb-task',
  'wb-edit',
  // credentials
  'api-key',
  'mailbox-host',
  'mailbox-user',
  'mailbox-secret',
  'github-login',
  // operation parameters and results
  'repo-owner',
  'repo-name',
  'branch',
  'file',
  'commit',
  'pr-title',
  'pr-body',
  'comment',
] as const;
type Label = (typeof LABELS)[number];

/**
 * Labels whose canaries the runner device must hold (positive control; 00-overview.md section 3: the execution device
 * sees plaintext): what the model double received (prompts), what the GitHub double received (repository, branch,
 * file, commit, pull request, comment) and what the session's model access used (the API key).
 */
const ON_RUNNER: readonly string[] = [
  'prompt/ascii',
  'prompt/unicode',
  'prompt-key/ascii',
  'prompt-key/unicode',
  'prompt-github/ascii',
  'prompt-github/unicode',
  'repo-owner/ascii',
  'repo-name/ascii',
  'branch/ascii',
  'file/ascii',
  'file/unicode',
  'commit/ascii',
  'commit/unicode',
  'pr-title/ascii',
  'pr-title/unicode',
  'pr-body/ascii',
  'pr-body/unicode',
  'comment/ascii',
  'comment/unicode',
];

interface ScanReport {
  readonly findings: readonly { source: string; label: string; encoding: string }[];
  readonly sources: Readonly<Record<string, number>>;
  readonly sse_frames: number;
  readonly ws_messages: number;
  readonly ws_failures: number;
  readonly probe: readonly string[];
  readonly notes: readonly string[];
}

/**
 * Every form of a 16-byte code a careless client could send (04-crypto.md section 1): the text shown, the text without
 * separators in both cases, and the decoded bytes as base64url and hex. All are canaries under `label/…`.
 */
function codeForms(label: string, text: string, bytes: Uint8Array): Record<string, string> {
  const bare = text.replace(/[- ]/g, '');
  const raw = Buffer.from(bytes);
  return {
    [`${label}-text`]: text,
    [`${label}-upper`]: bare.toUpperCase(),
    [`${label}-lower`]: bare.toLowerCase(),
    [`${label}-b64u`]: raw.toString('base64url'),
    [`${label}-hex`]: raw.toString('hex'),
  };
}

interface RunnerReport {
  readonly present: Readonly<Record<string, readonly string[]>>;
  readonly sources: Readonly<Record<string, number>>;
}

interface ColumnReport {
  readonly columns: readonly { table: string; column: string; category: string | null; content?: string }[];
  readonly categories: Readonly<Record<string, string>>;
  readonly problems: readonly string[];
}

/** Both canaries of `pair` are shown on `page` (decrypted in that browser). */
async function shows(page: Page, pair: CanaryPair, scope = page.locator('body')): Promise<void> {
  await expect(scope).toContainText(pair.ascii, { timeout: 30_000 });
  await expect(scope).toContainText(pair.unicode, { timeout: 30_000 });
}

test.describe.configure({ mode: 'serial' });

test('@E2E-09 @R-0.1 @R-5.1 @R-5.2 @R-5.3 @R-5.5 @R-12.5 @R-P.5 @needs-vm the hub sees no confidential content over the full flows; browser and runner do', async ({
  page,
  browser,
  e2e,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(1_800_000);
  const c = await newCanaries<Label>(orchestrator, LABELS);
  const device = await platformDevice(orchestrator);
  await poolReady(orchestrator);

  // E2E-01: the inviter (operator invitation), granted `inviter`, invites a new user through the platform screen.
  await authenticator('platform');
  const ada = await onboard(page, orchestrator, both(c['name-inviter']));
  const user = await PageUser.open(page, ada.recoveryCode);
  expect(await operator(orchestrator, ['grant-role', 'inviter', '--user', user.userId])).toContain(
    'appended role_grant',
  );
  await page.goto('/platform/invites');
  await page.getByTestId('login.submit').click();
  await page.getByTestId('platform-invites.open').click();
  await page.getByTestId('platform-invites.submit').click();
  const platformCode = (await page.getByTestId('platform-invites.code').textContent())!;
  await page.getByTestId('platform-invites.done').click();
  const inviteeContext = await browser.newContext({ baseURL: e2e.hubOrigin });
  const invitee = await inviteeContext.newPage();
  await addAuthenticator(invitee, 'platform');
  const kate = await onboardWithCode(invitee, platformCode, both(c['name-invitee']));
  await expect(invitee.getByTestId('home.screen')).toBeVisible();
  await addCanaryValues(orchestrator, {
    ...codeForms('code-operator-invite', ada.code, parseCode(ada.code)),
    ...codeForms('code-platform-invite', platformCode, parseCode(platformCode)),
    ...codeForms('code-recovery-inviter', ada.recoveryCode, parseRecoveryCode(ada.recoveryCode)),
    ...codeForms('code-recovery-invitee', kate.recoveryCode, parseRecoveryCode(kate.recoveryCode)),
  });

  // E2E-06: the first task; the session renames it (task.set_title) and answers.
  await page.goto(`/o/${ada.org}`);
  await page.getByTestId('login.submit').click();
  await expect(page.getByTestId('home.screen')).toBeVisible();
  await orchestrator.loadScenario(
    'model',
    fill(FIRST, { TITLE: both(c.title), ANSWER: both(c.answer), SIDE: both(c.side) }),
  );
  const prompt = `E2E09-FIRST ${both(c.prompt)}: what is 2+2?`;
  const task = await firstTask(page, orchestrator, device, prompt, c.answer.unicode);
  await shows(page, c.answer);
  await expect(page.getByTestId('task.title')).toHaveText(both(c.title), { timeout: 30_000 });

  // Names: the device (renamed), a workgroup, a member who joins with an org invitation.
  await navigateInApp(page, `/o/${ada.org}/devices`);
  await deviceItem(page, device).getByTestId('devices.item.rename').click();
  await page.getByTestId('devices.rename.input').fill(both(c['name-device']));
  await page.getByTestId('devices.rename.confirm').click();
  await expect(deviceItem(page, device).getByTestId('devices.item.name')).toHaveText(both(c['name-device']));
  const wg = await user.createWorkgroup(ada.org, both(c['name-wg']));
  const memberCode = await inviteMember(page, ada.org, { [wg]: 'read' });
  await addCanaryValues(orchestrator, codeForms('code-member-invite', memberCode, parseCode(memberCode)));
  const wendy = await joinAsNewUser(browser, e2e, ada.org, memberCode, both(c['name-member']));
  await addCanaryValues(
    orchestrator,
    codeForms('code-recovery-member', wendy.recoveryCode, parseRecoveryCode(wendy.recoveryCode)),
  );
  await navigateInApp(page, `/o/${ada.org}/members`);
  await shows(page, c['name-member'], page.getByTestId('members.item').filter({ hasText: c['name-member'].ascii }));

  // E2E-25: whiteboards on the three levels (an edit included); the member's browser reads them.
  await writeEntry(page, `/o/${ada.org}/whiteboard`, both(c['wb-org']));
  await writeEntry(page, `/o/${ada.org}/w/${wg}/whiteboard`, both(c['wb-wg']));
  const wgEntry = page.getByTestId('whiteboard.item').filter({ hasText: c['wb-wg'].ascii });
  await wgEntry.getByTestId('whiteboard.item.edit').click();
  await page.getByTestId('whiteboard.editor.text').fill(both(c['wb-edit']));
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(wgEntry).toHaveCount(0);
  await navigateInApp(page, `/o/${ada.org}/t/${task}`);
  await page.getByTestId('task.tabs.whiteboard').click();
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill(both(c['wb-task']));
  await page.getByTestId('whiteboard.editor.save').click();
  await shows(page, c['wb-task'], page.getByTestId('whiteboard.item'));
  await navigateInApp(wendy.page, `/o/${ada.org}/whiteboard`);
  await shows(wendy.page, c['wb-org'], wendy.page.getByTestId('whiteboard.item'));
  await navigateInApp(wendy.page, `/o/${ada.org}/w/${wg}/whiteboard`);
  await shows(wendy.page, c['wb-edit'], wendy.page.getByTestId('whiteboard.item'));
  await navigateInApp(wendy.page, `/o/${ada.org}/t/${task}`);
  await wendy.page.getByTestId('task.tabs.whiteboard').click();
  await shows(wendy.page, c['wb-task'], wendy.page.getByTestId('whiteboard.item'));
  await shows(wendy.page, c.title, wendy.page.getByTestId('task.title'));

  // E2E-27: credentials (API key, mailbox) stored encrypted; one turn with the user's API key.
  const apiKey = c['api-key'].ascii;
  await storeApiKey(page, apiKey);
  await openSettings(page);
  await page.getByTestId('mailbox.imap_host').fill(`${c['mailbox-host'].ascii}.example.org`);
  await page.getByTestId('mailbox.imap_user').fill(`${c['mailbox-user'].ascii}@example.org`);
  await page.getByTestId('mailbox.imap_password').fill(both(c['mailbox-secret']));
  await page.getByTestId('mailbox.email').fill(`${c['mailbox-user'].ascii}@example.org`);
  await page.getByTestId('mailbox.save').click();
  await expect(page.getByTestId('mailbox.saved')).toBeVisible({ timeout: 30_000 });
  await chooseModelAccess(page, 'api_key', CLAUDE_MODEL);
  await loadModelScenario(orchestrator, fill(API_KEY, { ANSWER: both(c['answer-key']), SIDE: both(c.side) }), {
    kind: 'api_key',
    token: apiKey,
  });
  await home(page);
  await sendPrompt(page, `E2E09-APIKEY ${both(c['prompt-key'])}: and with my key?`);
  await expectAnswerInChat(page, c['answer-key'].unicode);
  await expect.poll(async () => (await orchestrator.doubleState('model')).ok, { timeout: 60_000 }).toBe(true);
  await chooseModelAccess(page, 'platform');

  // E2E-21: the GitHub operation chain; every parameter and result carries canaries.
  const repo = `${c['repo-owner'].ascii}/${c['repo-name'].ascii}`;
  const branch = c.branch.ascii;
  const answer = `Draft pull request #1 is open: ${GITHUB_SITE}/${repo}/pull/1 ${both(c['answer-github'])}`;
  await loadModelScenario(
    orchestrator,
    fill(GITHUB, {
      REPO: repo,
      BRANCH: branch,
      FILE: both(c.file),
      COMMIT: both(c.commit),
      PR_TITLE: both(c['pr-title']),
      PR_BODY: both(c['pr-body']),
      COMMENT: both(c.comment),
      COMMENT_MARK: c.comment.ascii,
      ANSWER: both(c['answer-github']),
      SIDE: both(c.side),
    }),
    null,
  );
  await githubChain(page, orchestrator, ada, device, {
    login: c['github-login'].ascii,
    repo,
    branch,
    prompt: `E2E09-GITHUB ${both(c['prompt-github'])}: add notes to ${repo} and open a draft pull request`,
    answer,
    comment: both(c.comment),
  });
  await shows(page, c['answer-github']);

  // A further organization with its own name and the user's profile name in it.
  await page.getByTestId('org-switcher.current').click();
  await page.getByTestId('org-switcher.create.open').click();
  await page.getByTestId('org-switcher.create.name').fill(both(c['name-org']));
  await page.getByTestId('org-switcher.create.display-name').fill(both(c['name-profile']));
  await page.getByTestId('org-switcher.create.confirm').click();
  await expect(page.getByTestId('org-switcher.current')).toHaveText(both(c['name-org']), { timeout: 30_000 });

  // The GitHub token the double issued to the device (a credential the hub must never see). The double's journal
  // redacts it; its value is deterministic (`agent_doubles.github` `_issue_token`: "ghu_" and the first 36
  // alphanumerics of base64("token:<login>:<n>"), n counted from the seed), so the first issuances are registered.
  const authorized = (await githubJournal(orchestrator)).filter((call) =>
    /^Bearer <redacted 40 chars>$/.test(String((call as { headers?: Record<string, string> }).headers?.authorization)),
  );
  expect(authorized.length).toBeGreaterThan(0);
  const login = c['github-login'].ascii;
  const tokens = [1, 2, 3].map(
    (n) =>
      `ghu_${Buffer.from(`token:${login}:${n}`)
        .toString('base64')
        .replace(/[^A-Za-z0-9]/g, '')
        .padEnd(36, '0')
        .slice(0, 36)}`,
  );
  await addCanaryValues(
    orchestrator,
    Object.fromEntries(tokens.map((token, index) => [`github-token-${index + 1}`, token])),
  );

  // The checks: nothing at the hub, everything where it belongs.
  const scan = await orchestrator.call<ScanReport>('POST', '/canary/scan');
  const runner = await orchestrator.call<RunnerReport>('POST', '/canary/runner');
  const columns = await orchestrator.call<ColumnReport>('GET', '/canary/columns');
  const dir = join(e2e.iterationDir, 'e2e-09');
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, 'zero-knowledge.json'),
    JSON.stringify({ topology: e2e.topology, scan, runner, columns }, null, 1),
  );

  expect(scan.findings).toEqual([]);
  const sources = Object.keys(scan.sources);
  expect(scan.sources['hub-db-dump']).toBeGreaterThan(0);
  for (const pattern of [
    /^logs\/hub\.log$/,
    /^logs\/postgres\.log$/,
    /^browser-link\/\d+\.up$/,
    /^runner-link\/\d+\.up$/,
  ])
    expect(
      sources.some((source) => pattern.test(source)),
      String(pattern),
    ).toBe(true);
  for (const pattern of [/^browser-link\/\d+\.down$/, /^runner-link\/\d+\.down$/])
    expect(
      sources.some((source) => pattern.test(source)),
      String(pattern),
    ).toBe(true);
  // the organization exports (`agent-hub export`, 05 section 5.6) were scanned too: every export the orchestrator made
  const exports = sources.filter((source) => source.startsWith('export/'));
  expect(exports.length).toBeGreaterThan(0);
  expect(exports.every((source) => (scan.sources[source] ?? 0) > 0)).toBe(true);
  expect(scan.notes).toContain(`exports: ${exports.length} organization(s)`);
  expect(scan.sse_frames).toBeGreaterThan(0);
  expect(scan.ws_messages).toBeGreaterThan(0);
  expect(scan.ws_failures).toBe(0); // every WebSocket message was inflated and scanned as plaintext
  // the scan saw a probe that crossed every hub-side source on purpose
  for (const pattern of [
    /^hub-db-dump$/,
    /^logs\/hub\.log$/,
    /^logs\/postgres\.log$/,
    /^browser-link\/\d+\.up$/,
    /^runner-link\/\d+\.up$/,
  ])
    expect(
      scan.probe.some((source) => pattern.test(source)),
      `probe in ${String(pattern)}`,
    ).toBe(true);

  const missing = ON_RUNNER.filter((label) => !(label in runner.present));
  expect(missing, JSON.stringify(runner.present)).toEqual([]);

  expect(columns.problems).toEqual([]);
  expect(columns.columns.length).toBeGreaterThan(300);
  expect(columns.columns.every((column) => column.category !== null)).toBe(true);
  await inviteeContext.close();
});
