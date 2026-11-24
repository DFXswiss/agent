// E2E-46 (KOR-31, ADR-058, 12-billing.md): usage billing with prepaid credit over the whole chain. A private org on the
// platform device gets a start credit and a price list; its first task is charged for tokens through the platform key
// (`access: platform`) and for the VM runtime of the platform device, visible in the header, the settings section and
// the task tab "Costs". A new price list version applies only to later charges. With the credit used up the idle
// session is suspended with `credit_exhausted` and a new task waits with that reason; after a credit it starts and is
// answered. T1 only (one device for hub, runner and VMs).
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';
import {
  expectAnswerInChat,
  home,
  mainSessions,
  onboard,
  platformDevice,
  poolReady,
  sendPrompt,
  uuid,
} from '../fixtures/platform.ts';

const SCENARIO = JSON.parse(readFileSync(new URL('../scenarios/e2e-46-billing.json', import.meta.url), 'utf8')) as {
  name: string;
};
const PRICES = JSON.parse(
  readFileSync(new URL('../../../docs/platform/billing/price-list-2026-11-24.json', import.meta.url), 'utf8'),
) as Record<string, unknown>;
/** Without `valid_from`: a list applies from the moment it is added. */
const { valid_from: _validFrom, ...NOW_PRICES } = PRICES;

interface Billing {
  readonly rc: number;
  readonly lines: Record<string, unknown>[];
  readonly stderr: string;
}

const billing = (orchestrator: Orchestrator, args: string[], priceList?: unknown) =>
  orchestrator.call<Billing>(
    'POST',
    '/hub/billing',
    priceList === undefined ? { args } : { args, price_list: priceList },
  );

const charges = (orchestrator: Orchestrator, task: string) =>
  orchestrator.hubQuery<{ kind: string; price_version: number; access: string | null; amount: number }>(
    `SELECT kind, price_version, quantity->>'access' AS access, amount_micro_usd AS amount FROM hub.charge` +
      ` WHERE task_id = '${uuid(task)}' ORDER BY at`,
  );

test.describe.configure({ mode: 'serial' });

test.beforeEach(({ e2e }) => {
  test.skip(e2e.topology !== 't1', 'E2E-46 is a T1 test');
});

test('@E2E-46 @R-P.14 @needs-vm usage is charged from the prepaid credit; used up pauses and waits, a credit continues', async ({
  page,
  orchestrator,
  authenticator,
}) => {
  test.setTimeout(900_000);
  await platformDevice(orchestrator);
  await poolReady(orchestrator);
  await authenticator('platform');
  const { org } = await onboard(page, orchestrator, 'Ada E2E46');
  await orchestrator.loadScenario('model', SCENARIO);

  // Start credit and the first price list, as in the runbook.
  // Without a USD/CHF rate billing cannot be switched on; the rate first, as in the runbook.
  const refused = await billing(orchestrator, ['price-list', 'add', '--file', '@price_list'], NOW_PRICES);
  expect(refused.rc).toBe(1);
  expect(refused.stderr).toContain('no USD/CHF rate is set');
  const [today] = await orchestrator.hubQuery<{ d: string }>(
    "SELECT to_char(now() AT TIME ZONE 'Europe/Zurich', 'YYYY-MM-DD') AS d",
  );
  const rate = await billing(orchestrator, ['fx', 'set', '--day', today!.d, '--rate', '0.8486', '--source', 'BAZG']);
  expect(rate.rc, rate.stderr).toBe(0);
  await orchestrator.call('POST', '/hub/start-credit', { usd: '5' });
  const added = await billing(orchestrator, ['price-list', 'add', '--file', '@price_list'], NOW_PRICES);
  expect(added.rc, added.stderr).toBe(0);
  await expect
    .poll(async () => (await billing(orchestrator, ['show', '--org', org])).lines[0]?.account, { timeout: 30_000 })
    .toEqual({
      balance: 5_000_000,
      booked: 5_000_000,
    });

  // (1) The first task: tokens of the platform key and the runtime of the platform VM are charged.
  await home(page);
  const first = await sendPrompt(page, 'E2E46-FIRST: what does this cost?');
  await expectAnswerInChat(page, 'First answer kz46-first-h3m8q1.');
  await expect
    .poll(async () => (await charges(orchestrator, first)).map((row) => row.kind), { timeout: 120_000 })
    .toEqual(expect.arrayContaining(['tokens', 'runtime']));
  const booked = await charges(orchestrator, first);
  expect(
    booked.filter((row) => row.kind === 'tokens').every((row) => row.access === 'platform' && row.price_version === 1),
  ).toBe(true);
  await expect(page.getByTestId('credit.badge')).toHaveText(/^Credit \$\d/, { timeout: 90_000 });
  await page.getByTestId('task.tabs.charges').click();
  await expect(page.getByTestId('charges.total')).toHaveText(/^Total \$/);

  // Every booking keeps its CHF equivalent at the rate of its day (books only; the web shows USD).
  const equivalents = await orchestrator.hubQuery<{ ok: boolean }>(
    `SELECT bool_and(fx_day = '${today!.d}' AND chf_per_usd_e8 = 84860000 AND amount_micro_chf IS NOT NULL) AS ok` +
      ` FROM hub.charge WHERE task_id = '${uuid(first)}'`,
  );
  expect(equivalents[0]!.ok).toBe(true);
  await expect(page.getByTestId('charges.total')).not.toContainText('CHF');

  // (2) A new price list version applies only to what is measured after it.
  const doubled = { ...NOW_PRICES, tokens: { ...(PRICES.tokens as object), factors: { platform: 2.12, operator: 2 } } };
  const second = await billing(orchestrator, ['price-list', 'add', '--file', '@price_list'], doubled);
  expect(second.rc, second.stderr).toBe(0);
  await expect
    .poll(async () => (await charges(orchestrator, first)).some((row) => row.price_version === 2), { timeout: 120_000 })
    .toBe(true);
  // what was booked before stays as it was; once version 2 is in force no further charge uses version 1
  const versionOne = (await charges(orchestrator, first)).filter((row) => row.price_version === 1);
  expect(versionOne.slice(0, booked.length)).toEqual(booked);
  await page.waitForTimeout(12_000); // more than two runtime meter ticks
  expect((await charges(orchestrator, first)).filter((row) => row.price_version === 1)).toHaveLength(versionOne.length);

  // (3) Credit used up: the idle session is suspended, a new task waits with "credit used up".
  const [session] = await mainSessions(orchestrator, first);
  expect((await billing(orchestrator, ['credit', '--org', org, '--set', '0', '--reason', 'e2e used up'])).rc).toBe(0);
  await expect
    .poll(
      async () =>
        (
          await orchestrator.hubQuery<{ reason: string | null }>(
            `SELECT reason FROM hub.session_transition WHERE session_id = '${uuid(session!.id)}' AND state = 'suspended'`,
          )
        ).map((row) => row.reason),
      { timeout: 120_000 },
    )
    .toContain('credit_exhausted');
  await expect(page.getByTestId('credit.badge')).toHaveText('Credit used up', { timeout: 90_000 });
  await home(page);
  const waiting = await sendPrompt(page, 'E2E46-AFTER: and now?');
  await expect
    .poll(
      async () =>
        (
          await orchestrator.hubQuery<{ state: string; state_reason: string | null }>(
            `SELECT state, state_reason FROM hub.task WHERE id = '${uuid(waiting)}'`,
          )
        )[0],
      { timeout: 60_000 },
    )
    .toEqual({ state: 'scheduling', state_reason: 'credit_exhausted' });

  // (4) A credit: the waiting task starts and is answered.
  expect((await billing(orchestrator, ['credit', '--org', org, '--add', '5', '--reason', 'e2e credit'])).rc).toBe(0);
  await expectAnswerInChat(page, 'Answer after the credit kz46-after-p6t2w9.');
  const history = await orchestrator.hubQuery<{ kind: string }>(
    `SELECT kind FROM hub.credit_entry WHERE org_id = '${uuid(org)}' ORDER BY n`,
  );
  expect(history.map((row) => row.kind)).toEqual(['start', 'set', 'grant']);
});
