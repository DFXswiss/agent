// Fixtures of the billing suites: an answer of `GET /v1/orgs/{org}/billing` with every kind of malformed row.
export const totals = { tokens_micro_usd: 445_200, runtime_micro_usd: 3000, tokens: 1_100_000, runtime_ms: 30_000 };
export const BILLING = {
  currency: 'USD',
  balance_micro_usd: 4_551_800,
  exhausted: false,
  billing_active: true,
  price_list: {
    version: 1,
    valid_from: '2026-11-21T00:00:00.000Z',
    body: {
      currency: 'USD',
      tokens: {
        factors: { platform: 2, operator: 1 },
        models: { 'deepseek/flash': { input: 0.3, output: 1.2, cache_read: 0, cache_write: 0.3 }, broken: 'x' },
        default: { input: 8, output: 40, cache_read: 0.2, cache_write: 10 },
      },
      runtime: { per_hour: { session: 0.36, large: 0.72, default: 1.44 } },
    },
  },
  credits: [
    {
      at: '2026-11-21T12:00:00.000Z',
      kind: 'grant',
      amount_micro_usd: 2_000_000,
      balance_micro_usd: 7e6,
      reason: 'hi',
    },
    { at: '2026-11-21T11:00:00.000Z', kind: 'start', amount_micro_usd: 5e6, balance_micro_usd: 5e6, reason: null },
    { kind: 'broken' },
    'broken',
  ],
  days: [{ day: '2026-11-21', ...totals }, { day: 5, ...totals }, { day: '2026-11-20' }],
  workgroups: [{ wg_id: 'wg-1', ...totals }, { ...totals }],
};

/** `people` of ADR-083 with malformed rows: a person, unattributed consumption, a bad user and a bad amount. */
export const PEOPLE = [
  { user_id: 'u-1', ...totals },
  { user_id: null, ...totals },
  { user_id: 5, ...totals },
  { user_id: 'u-2', tokens: 'x' },
  'broken',
];
