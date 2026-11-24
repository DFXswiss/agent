// E2E-00 (M0 smoke, ADR-027 section 7): the hub of the integration state serves the web build; a virtual
// authenticator with PRF creates a credential on the hub origin and yields 32-byte PRF outputs on create() and get()
// (04-crypto.md section 3); the orchestrator's model double and runner process are up. Runs on T1 and T2. Full
// passkey registration through the hub API follows in M1 (SCN-01).
import { readFileSync } from 'node:fs';
import { expect, test } from '../fixtures/test.ts';
import { PRF_INPUT_TEXT } from '../fixtures/authenticator.ts';
import type { Orchestrator } from '../fixtures/orchestrator.ts';

const scenario: unknown = JSON.parse(readFileSync(new URL('../scenarios/e2e-00-smoke.json', import.meta.url), 'utf8'));
/**
 * Every runner of the run is up: waiting for its enrollment, or - only when the hub already has an enrolled device
 * (an earlier spec of the same run enrolled it, e.g. PERF-01/-02 or E2E-05) - running.
 */
async function expectRunnersUp(orchestrator: Orchestrator, runners: Record<string, string>): Promise<void> {
  expect(Object.keys(runners).length).toBeGreaterThan(0);
  const [row] = await orchestrator.hubQuery<{ devices: number }>(`SELECT count(*)::int AS devices FROM hub.device`);
  const allowed = row!.devices > 0 ? ['waiting_for_enrollment', 'running'] : ['waiting_for_enrollment'];
  for (const runnerState of Object.values(runners)) expect(allowed).toContain(runnerState);
}

interface PrfResult {
  readonly enabled: boolean | null;
  readonly createLength: number | null;
  readonly getLength: number | null;
  readonly same: boolean;
}

/** Runs in the page: create() and get() with `prf.eval.first`; lengths of both outputs and whether they agree. */
async function prfCeremonies(input: string): Promise<PrfResult> {
  const salt = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(input)));
  const random = (n: number) => crypto.getRandomValues(new Uint8Array(n));
  const created = (await navigator.credentials.create({
    publicKey: {
      rp: { id: location.hostname, name: 'agent.dfx.swiss E2E' },
      user: { id: random(16), name: 'e2e-smoke', displayName: 'E2E smoke' },
      challenge: random(32),
      pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
      authenticatorSelection: { residentKey: 'required', userVerification: 'required' },
      extensions: { prf: { eval: { first: salt } } },
    },
  })) as PublicKeyCredential;
  const atCreate = created.getClientExtensionResults().prf;
  const asserted = (await navigator.credentials.get({
    publicKey: {
      rpId: location.hostname,
      challenge: random(32),
      allowCredentials: [{ type: 'public-key', id: created.rawId }],
      userVerification: 'required',
      extensions: { prf: { eval: { first: salt } } },
    },
  })) as PublicKeyCredential;
  const atGet = asserted.getClientExtensionResults().prf;
  const bytes = (value: BufferSource | undefined) =>
    value === undefined ? null : new Uint8Array(value instanceof ArrayBuffer ? value : value.buffer);
  const first = bytes(atCreate?.results?.first);
  const second = bytes(atGet?.results?.first);
  return {
    enabled: atCreate?.enabled ?? null,
    createLength: first?.byteLength ?? null,
    getLength: second?.byteLength ?? null,
    same: first !== null && second !== null && first.every((byte, i) => byte === second[i]),
  };
}

test.describe.configure({ mode: 'serial' });

test('@E2E-00 the hub serves the web build and a platform authenticator yields 32-byte PRF outputs', async ({
  page,
  e2e,
  orchestrator,
  authenticator,
}) => {
  const state = await orchestrator.state();
  expect(state.hub.origin).toBe(e2e.hubOrigin);
  await expectRunnersUp(orchestrator, state.runners);

  const config = page.waitForResponse((response) => new URL(response.url()).pathname === '/v1/config');
  const response = await page.goto('/');
  expect(response?.status()).toBe(200);
  expect(response?.headers()['content-security-policy']).toContain("script-src 'self'");
  const configResponse = await config;
  expect(configResponse.status()).toBe(200);
  expect(await configResponse.json()).toMatchObject({ origin: e2e.hubOrigin, rp_id: new URL(e2e.hubOrigin).hostname });
  await expect(page.locator('#root')).not.toBeEmpty();
  await expect(page.getByTestId('error.page')).toHaveCount(0);

  await authenticator('platform');
  expect(await page.evaluate(prfCeremonies, PRF_INPUT_TEXT)).toEqual({
    enabled: true,
    createLength: 32,
    getLength: 32,
    same: true,
  });
});

test('@E2E-00 the model double consumes its scenario and the runners are up', async ({ orchestrator }) => {
  await orchestrator.loadScenario('model', scenario);
  const answer = await orchestrator.doubleRequest('model', 'POST', '/v1/messages', {
    model: 'deepseek/deepseek-v4.1-flash-1017',
    max_tokens: 16,
    stream: false,
    messages: [{ role: 'user', content: 'orchestrator smoke probe' }],
  });
  expect(answer.status).toBe(200);
  expect(JSON.stringify(answer.body)).toContain('smoke ok');
  const double = await orchestrator.doubleState('model');
  expect(double.ok).toBe(true);
  await expectRunnersUp(orchestrator, await orchestrator.runners());
  const links = await orchestrator.links();
  // Every stack runs the model and the GitHub double on the runner device (SCN-04 added the GitHub link).
  expect(Object.keys(links).sort()).toEqual(['browser-hub', 'github', 'model', 'runner-hub']);
  // The browser reached the hub through its relay; the runner device through the runner relay (T2: over the LAN).
  expect(links['browser-hub']!.connections).toBeGreaterThan(0);
  expect(links['runner-hub']!.connections).toBeGreaterThan(0);
});

test('@E2E-00 the authenticator variants: USB key with PRF, PRF-less key, refused user verification', async ({
  page,
  authenticator,
}) => {
  await page.goto('/');
  const usb = await authenticator('usb');
  expect((await page.evaluate(prfCeremonies, PRF_INPUT_TEXT)).createLength).toBe(32);
  await usb.remove();

  const noPrf = await authenticator('no-prf');
  const withoutPrf = await page.evaluate(prfCeremonies, PRF_INPUT_TEXT);
  expect(withoutPrf.createLength).toBeNull();
  expect(withoutPrf.getLength).toBeNull();
  expect(await noPrf.credentials()).toHaveLength(1);
  await noPrf.remove();

  // Last: Chromium keeps a refused user verification for later ceremonies of the page (Spike D).
  await authenticator('uv-refused');
  const refused = await page.evaluate(
    (input) =>
      navigator.credentials
        .create({
          publicKey: {
            rp: { id: location.hostname, name: 'agent.dfx.swiss E2E' },
            user: { id: crypto.getRandomValues(new Uint8Array(16)), name: 'uv', displayName: 'uv' },
            challenge: crypto.getRandomValues(new Uint8Array(32)),
            pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
            authenticatorSelection: { residentKey: 'required', userVerification: 'required' },
            extensions: { prf: { eval: { first: new TextEncoder().encode(input) } } },
          },
        })
        .then(
          () => 'created',
          (error: Error) => error.name,
        ),
    PRF_INPUT_TEXT,
  );
  expect(refused).toBe('NotAllowedError');
});
