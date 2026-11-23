// U-WEB-credentials (WEB-10): the Claude login (relay with the code as follow-up, mailbox automation with progress and
// fallback) and the credential checks as sealed device requests, and the flow states read from the opened responses.
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { openRequest } from '../../../src/core/crypto/request';
import {
  AUTOMATION_FAILED,
  checkProgress,
  loginProgress,
  sendCode,
  startCheck,
  startLogin,
} from '../../../src/core/credentials/login';
import type { OpenedResponse, RequestView } from '../../../src/core/requests/client';
import { credWorld, type CredWorld } from './world';

function accept(t: CredWorld) {
  t.client.on('POST /v1/device-requests', (body) => {
    const id = (body as { envelope: { header: { id: string } } }).envelope.header.id;
    return { request_id: id, state: 'pending' };
  });
}

async function opened(t: CredWorld, index: number) {
  const envelope = (t.client.of('POST /v1/device-requests')[index]!.body as { envelope: never }).envelope;
  const plaintext = await openRequest(t.device.identity.kem, envelope, b64uDecode(t.me.identity.public().sig));
  return { header: (envelope as { header: Record<string, unknown> }).header, body: JSON.parse(fromUtf8(plaintext)) };
}

const ok = (seq: number, body: unknown, final = false): OpenedResponse =>
  ({ seq, status: 'verified', final, at: 0, body }) as OpenedResponse;
const view = (state: string, responses: OpenedResponse[], id = 'r1'): RequestView => ({
  id,
  state,
  expiresAt: 0,
  responses,
});

describe('requests', () => {
  it('sends the login in both modes and the code as a follow-up of the original', async () => {
    const t = await credWorld();
    accept(t);
    const original = await startLogin(t.context, t.device.id, 'manual');
    expect(await opened(t, 0)).toMatchObject({
      header: { kind: 'claude.login', follows: '', device: t.device.id },
      body: { mode: 'manual' },
    });
    const followUp = await sendCode(t.context, original, '  abc-123#state  ');
    expect(followUp.follows).toBe(original.id);
    expect(await opened(t, 1)).toMatchObject({
      header: { kind: 'claude.login', follows: original.id },
      body: { code: 'abc-123#state' },
    });
    await startLogin(t.context, t.device.id, 'automatic');
    expect((await opened(t, 2)).body).toEqual({ mode: 'automatic' });
  });

  it('sends the checks of the API key, the subscription and the mailbox', async () => {
    const t = await credWorld();
    accept(t);
    await startCheck(t.context, t.device.id, 'anthropic_api_key');
    await startCheck(t.context, t.device.id, 'claude_subscription');
    await startCheck(t.context, t.device.id, 'mailbox');
    expect(await opened(t, 0)).toMatchObject({
      header: { kind: 'credential.check' },
      body: { kind: 'anthropic_api_key' },
    });
    expect((await opened(t, 1)).body).toEqual({ kind: 'claude_subscription' });
    expect(await opened(t, 2)).toMatchObject({ header: { kind: 'mailbox.check' }, body: {} });
  });
});

describe('loginProgress', () => {
  it('follows the relay: waiting, authorize, then done from the follow-up', () => {
    expect(loginProgress(view('pending', []), null)).toEqual({ step: 'waiting' });
    const authorize = ok(1, { step: 'authorize', url: 'https://claude.ai/oauth/authorize?x=1' });
    expect(loginProgress(view('delivered', [authorize]), null)).toEqual({
      step: 'authorize',
      url: 'https://claude.ai/oauth/authorize?x=1',
      automationFailed: false,
    });
    expect(loginProgress(view('delivered', [authorize]), view('pending', [], 'r2'))).toMatchObject({
      step: 'authorize',
    });
    const done = view('answered', [ok(1, { step: 'done', email: 'claude@example.com' }, true)], 'r2');
    expect(loginProgress(view('delivered', [authorize]), done)).toEqual({
      step: 'done',
      email: 'claude@example.com',
    });
    expect(loginProgress(view('answered', [ok(1, { step: 'done' }, true)]), null)).toEqual({
      step: 'done',
      email: null,
    });
  });

  it('shows the automation progress and its fallback to the relay', () => {
    const progress = ok(1, { step: 'progress', stage: 'magic_link_received' });
    expect(loginProgress(view('delivered', [progress]), null)).toEqual({
      step: 'progress',
      stage: 'magic_link_received',
    });
    const fallback = ok(2, { step: 'authorize', url: 'https://claude.ai/x', automation: 'failed' });
    expect(loginProgress(view('delivered', [progress, fallback]), null)).toEqual({
      step: 'authorize',
      url: 'https://claude.ai/x',
      automationFailed: true,
    });
    expect(AUTOMATION_FAILED).toBe('login_automation_failed');
  });

  it('reports failures of the device and of the hub, and ignores unverifiable responses', () => {
    expect(loginProgress(view('answered', [ok(1, { step: 'failed', code: 'code_invalid' }, true)]), null)).toEqual({
      step: 'failed',
      code: 'code_invalid',
    });
    const authorize = ok(1, { step: 'authorize', url: 'https://claude.ai/x' });
    expect(loginProgress(view('expired', [authorize]), null)).toEqual({ step: 'failed', code: 'timeout' });
    expect(loginProgress(view('delivered', [authorize]), view('failed', [], 'r2'))).toEqual({
      step: 'failed',
      code: 'request_failed',
    });
    const forged: OpenedResponse = { seq: 2, status: 'unverifiable', code: 'signature_invalid' };
    expect(loginProgress(view('delivered', [authorize, forged]), null)).toMatchObject({ step: 'authorize' });
    // a body of another kind never reaches the flow (bodies.ts); were it there, it would count as no answer
    expect(loginProgress(view('delivered', [ok(1, { step: 'device_code' })]), null)).toEqual({ step: 'waiting' });
  });
});

describe('checkProgress', () => {
  it('reads valid, invalid, failures and hub ends', () => {
    expect(checkProgress(view('pending', []))).toEqual({ step: 'waiting' });
    expect(checkProgress(view('answered', [ok(1, { step: 'done', valid: true }, true)]))).toEqual({
      step: 'done',
      valid: true,
    });
    expect(checkProgress(view('answered', [ok(1, { step: 'done', valid: false }, true)]))).toEqual({
      step: 'done',
      valid: false,
    });
    expect(checkProgress(view('answered', [ok(1, { step: 'failed', code: 'credential_missing' }, true)]))).toEqual({
      step: 'failed',
      code: 'credential_missing',
    });
    expect(checkProgress(view('expired', []))).toEqual({ step: 'failed', code: 'timeout' });
  });
});
