// U-WEB-invite-org (screen part, 06-web.md section 3.1): the org path of `/invite#<code>` in the real app against the
// simulated hub, with an org of the core test world behind it (real package, real log, the hub's batch rules for the
// `accept`): a new user (passkey; a recovery code only where one is shown, KOR-22), an existing user (signed in, locked, signing in from the intro,
// recovery-code unlock), cancelled gestures, blocking refusals of the package and the log, invalid invitations,
// `invite_registered` and a failed `accept` with retry.
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { OrgInvitation } from '../../src/core/account/join';
import { signIn } from '../../src/core/account/login';
import { codeToInviteId, register } from '../../src/core/account/register';
import { fingerprint } from '../../src/core/crypto/agent';
import { Identity } from '../../src/core/crypto/keys';
import { b64uEncode } from '../../src/core/encoding/b64u';
import { formatTime } from '../../src/core/encoding/time';
import { uuid7 } from '../../src/core/encoding/ids';
import { CryptoError } from '../../src/core/crypto/errors';
import { formatCode, inviteSecrets } from '../../src/core/crypto/invite';
import { queuedSelfWraps } from '../../src/core/jobs/self-wraps';
import type { Entry } from '../../src/core/log/entries';
import { HubError } from '../../src/core/net/signedFetch';
import { createInvite } from '../../src/core/org/invites';
import { orgEntry } from '../../src/core/store/slices/orgs';
import { forgetFragment } from '../../src/ui/screens/invite/fragment';
import { afterVerify, blocking, failedStep, verifyFailed } from '../../src/ui/screens/invite/org/machine';
import { FakeAuthenticator, giveRecoveryCode } from '../core/account/authenticator';
import { HubFailure, ORIGIN, SimHub } from '../core/account/hub';
import { idOf } from '../core/log/world';
import { orgWorld, type OrgWorld } from '../core/org/world';
import { type makeApp, renderApp } from './harness';

type Made = Awaited<ReturnType<typeof makeApp>>;

const CODE_BYTES = new Uint8Array(16).fill(3);
const CODE = formatCode(CODE_BYTES);
const HUB_KEY = { id: 'hub-test', public_key: b64uEncode(new Uint8Array(32).fill(9)) };

afterEach(() => forgetFragment());

interface OrgSetup {
  readonly w: OrgWorld;
  readonly one: string;
  readonly inviteId: string;
  readonly expires: string;
  /** Changes the next lookup answers. */
  patch: ((raw: Record<string, unknown>) => void) | null;
}

/** An org of the core test world with an open user invitation (code `CODE`), wired into `hub`. */
async function orgWithInvite(hub: SimHub, names = true): Promise<OrgSetup> {
  const one = await idOf('wg one');
  const w = await orgWorld({
    setup: async (world, a) => {
      await world.batch(a.ada!, [{ type: 'wg_create', wg: one, epoch: 1 }]);
    },
  });
  const created = await createInvite(w.context, w.org, {
    request: { kind: 'user', role: 'member', wgs: { [one]: 'read' }, hours: 72 },
    directory: {
      orgName: names ? 'Grüße AG' : '',
      workgroups: names
        ? new Map([
            [w.world.defaultWg, 'Default'],
            [one, 'One'],
          ])
        : new Map(),
      members: new Map(),
    },
    hubKey: HUB_KEY,
    origin: ORIGIN,
    code: () => CODE_BYTES,
  });
  const setup: OrgSetup = { w, one, inviteId: created.inviteId, expires: created.expires, patch: null };
  const { pkInv } = await inviteSecrets(CODE_BYTES);
  hub.invites.set(created.inviteId, { scope: 'org', kind: 'user', registered: null, used: false, pkInv });
  hub.override.set('POST /v1/invites/lookup', () => {
    const invite = hub.invites.get(created.inviteId)!;
    if (invite.used) throw new HubFailure(404, 'invite_unknown');
    const log = w.world.log!;
    const raw: Record<string, unknown> = {
      kind: 'user',
      scope: 'org',
      org_id: w.org,
      expires_at: created.expires,
      registered: invite.registered !== null,
      package_ct: w.hub.invites.get(created.inviteId)!.package_ct,
      log_head: { ...log.head },
      log: log.entries.map((entry) => structuredClone(entry)),
    };
    setup.patch?.(raw);
    return raw;
  });
  hub.override.set(`POST /v1/orgs/${w.org}/log`, async (raw) => {
    const body = raw as { entries: Entry[]; wraps: Record<string, unknown>[] };
    const by = body.entries[0]!.body.by as string;
    let answer;
    try {
      answer = await w.hub.append(by, body as never);
    } catch (error) {
      if (error instanceof HubError || error instanceof CryptoError) throw new HubFailure(409, error.code);
      throw error;
    }
    hub.invites.get(created.inviteId)!.used = true;
    // the log job's incremental read after the accept (`?after=<head>`; the sim hub itself ignores `after`)
    const head = { ...w.world.log!.head };
    hub.override.set(`GET /v1/orgs/${w.org}/log`, () => ({ entries: [], head, next_after: null }));
    hub.users.get(by)!.orgs.push(w.org);
    hub.orgs.set(w.org, {
      id: w.org,
      defaultWg: w.world.defaultWg,
      owner: w.me.id,
      entries: [...w.world.log!.entries],
      wraps: body.wraps.map((wrap) => ({ ...wrap, wrapped_by: by })),
      events: [],
    });
    return answer;
  });
  return setup;
}

/** Opens `/invite#CODE` with an org invitation; `before` prepares the app (sign-in …). */
async function openOrgInvite(
  options: { hub?: SimHub; auth?: FakeAuthenticator; names?: boolean; suffix?: string } = {},
  before?: (made: Made, setup: OrgSetup) => Promise<void>,
) {
  const hub = options.hub ?? new SimHub();
  const setup = await orgWithInvite(hub, options.names);
  window.history.replaceState({ key: 'invite' }, '', `/invite#${CODE}${options.suffix ?? ''}`);
  const harness = await renderApp('/invite', { hub, auth: options.auth ?? new FakeAuthenticator() }, async (made) => {
    if (before) await before(made, setup);
    else await made.app.restore();
  });
  return { ...harness, setup };
}

/** A registered user of `made.hub` (its platform invitation), signed in; `unlock`/`slot` as asked. */
async function existingUser(made: Made, options: { unlock?: boolean; slot?: boolean } = {}) {
  const registered = await register(made.app.anon, made.auth, made.hub.addInvite());
  // an account with a recovery code (the fallbacks below type it)
  const registration = { ...registered, recoveryCode: await giveRecoveryCode(made.hub, registered) };
  const login = await signIn({ anon: made.app.anon, credentials: made.auth, origin: ORIGIN });
  await made.app.begin(login);
  if (options.unlock !== false || options.slot) await made.app.unlock(registration.identity);
  if (options.slot) await made.app.uploadPasskeySlot(login, registration.identity);
  return registration;
}

const accepted = (setup: OrgSetup, userId: string) => setup.w.world.state.member(userId) !== null;

async function expectJoined(harness: Awaited<ReturnType<typeof openOrgInvite>>, user: string | (() => string)) {
  const { setup, app, persist, router } = harness;
  await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${setup.w.org}`));
  const userId = typeof user === 'string' ? user : user();
  expect(accepted(setup, userId)).toBe(true);
  // the joined org: its founder pinned (not the user's key), the package keys in memory, the last org
  expect((await persist.loadPinned(setup.w.org))!.founder).toBe(setup.w.world.state.founder);
  expect(orgEntry(app.store.getState().orgs, setup.w.org).keys.size).toBe(3);
  expect(app.landingOrg()).toBe(setup.w.org);
  // the batch carried the self-wraps of the current epochs, so the job finds nothing left to send
  await waitFor(() => expect(queuedSelfWraps(app.store, setup.w.org)).toBeNull());
}

describe('org invitation steps', () => {
  it('classify failures', () => {
    expect(failedStep(new HubError('invite_registered', 409))).toEqual({ step: 'invalid', code: 'invite_registered' });
    expect(failedStep(new CryptoError('invite_revoked'))).toEqual({ step: 'invalid', code: 'invite_unknown' });
    expect(failedStep(new HubError('not_found', 404))).toEqual({ step: 'invalid', code: 'invite_unknown' });
    expect(failedStep(new CryptoError('log_fork'))).toEqual({ step: 'blocked', code: 'log_fork' });
    expect(failedStep(new CryptoError('invite_not_in_log'))).toEqual({ step: 'blocked', code: 'invite_not_in_log' });
    // a refusal of the hub is no evidence against the log; the step shows it with a retry
    expect(failedStep(new HubError('log_rights', 403))).toBeNull();
    expect(failedStep(new CryptoError('log_accept'))).toBeNull();
    expect(failedStep(new Error('x'))).toBeNull();
    expect(blocking('log_chain')).toBe(true);
    expect(blocking('log_accept')).toBe(false);
    expect(blocking('unavailable')).toBe(false);
    expect(verifyFailed(new Error('x'))).toEqual({ step: 'blocked', code: 'unavailable' });
    expect(verifyFailed(new HubError('invite_expired', 410))).toEqual({ step: 'invalid', code: 'invite_expired' });
  });

  it('go to the existing account for a session', () => {
    const invitation = {} as OrgInvitation;
    expect(afterVerify(invitation, 'unlocked')).toMatchObject({ step: 'existing_account', mode: 'ready' });
    expect(afterVerify(invitation, 'locked')).toMatchObject({ step: 'existing_account', mode: 'sign_in' });
    expect(afterVerify(invitation, 'anonymous')).toEqual({ step: 'intro', invitation, notice: null });
  });
});

describe('OrgInvite: new user', () => {
  it('shows the invitation, registers and joins without a code screen (KOR-22)', async () => {
    const harness = await openOrgInvite();
    expect(await screen.findByTestId('invite.org-name')).toHaveTextContent('You are invited to Grüße AG');
    expect(screen.getByTestId('invite.role')).toHaveTextContent('Your role: Member');
    const wgs = screen.getAllByTestId('invite.workgroup');
    expect(wgs.map((item) => [item.textContent, item.dataset.access])).toEqual([
      ['Default – read and write', 'write'],
      ['One – read', 'read'],
    ]);
    expect(screen.getByTestId('invite.expires')).toHaveTextContent('The invitation is valid until');
    expect(window.location.hash).toBe('');
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    expect(screen.getByTestId('invite.display-name.invalid')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('invite.display-name'), { target: { value: 'Erin' } });
    expect(screen.queryByTestId('invite.display-name.invalid')).toBeNull();
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    fireEvent.click(await screen.findByTestId('invite.unlock'));
    await waitFor(() => expect(harness.app.userId).not.toBeNull());
    const userId = harness.app.userId!;
    await expectJoined(harness, userId);
    expect(screen.queryByTestId('recovery-code.step')).toBeNull();
    expect(harness.setup.w.hub.events).toHaveLength(1);
  });

  it('returns after cancelled gestures, refuses another account and then joins', async () => {
    const auth = new FakeAuthenticator();
    const harness = await openOrgInvite({ auth, names: false });
    expect(await screen.findByTestId('invite.org-name')).toHaveTextContent('You are invited to an organization');
    expect(screen.getAllByTestId('invite.workgroup').map((item) => item.textContent)).toEqual([
      'Unnamed workgroup – read and write',
      'Unnamed workgroup – read',
    ]);
    fireEvent.change(screen.getByTestId('invite.display-name'), { target: { value: 'Erin' } });
    auth.refuse = 'AbortError';
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    expect(await screen.findByTestId('invite.notice')).toHaveTextContent('Passkey creation was cancelled');
    harness.hub.override.set('POST /v1/passkeys/register/finish', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    await waitFor(() => expect(screen.getByTestId('invite.notice')).toHaveTextContent('Service unavailable'));
    harness.hub.override.delete('POST /v1/passkeys/register/finish');
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    await screen.findByTestId('invite.unlock');
    const mine = auth.credentials[auth.credentials.length - 1]!;
    await register(harness.app.anon, auth, harness.hub.addInvite());
    auth.use = auth.credentials[auth.credentials.length - 1]!;
    fireEvent.click(screen.getByTestId('invite.unlock'));
    expect(await screen.findByTestId('invite.notice')).toHaveTextContent('belongs to another account');
    auth.use = mine;
    auth.refuse = 'NotAllowedError';
    fireEvent.click(screen.getByTestId('invite.unlock'));
    await waitFor(() => expect(screen.getByTestId('invite.notice')).toHaveTextContent('Sign-in was cancelled'));
    harness.hub.override.set('GET /v1/me', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('invite.unlock'));
    await waitFor(() => expect(screen.getByTestId('invite.notice')).toHaveTextContent('Service unavailable'));
    harness.hub.override.delete('GET /v1/me');
    fireEvent.click(screen.getByTestId('invite.unlock'));
    await waitFor(() => expect(harness.router.state.location.pathname).toBe(`/o/${harness.setup.w.org}`));
  });

  it('ends in the invalid state when the hub refuses the registration', async () => {
    const harness = await openOrgInvite();
    fireEvent.change(await screen.findByTestId('invite.display-name'), { target: { value: 'Erin' } });
    harness.hub.override.set('POST /v1/passkeys/register/begin', new HubFailure(404, 'invite_unknown'));
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    expect(await screen.findByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_unknown');
  });

  it('without PRF ends with the message, withdraws the registration and lets the same link work again', async () => {
    const auth = new FakeAuthenticator();
    auth.prf = false;
    const harness = await openOrgInvite({ auth });
    fireEvent.change(await screen.findByTestId('invite.display-name'), { target: { value: 'Erin' } });
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    fireEvent.click(await screen.findByTestId('invite.unlock'));
    expect(await screen.findByTestId('prf-required')).toHaveTextContent('You need a passkey that supports PRF.');
    expect(harness.hub.users.size).toBe(0);
    expect([...harness.hub.invites.values()].every((invite) => invite.registered === null)).toBe(true);
    expect(accepted(harness.setup, harness.setup.w.me.id)).toBe(true);
    fireEvent.click(screen.getByTestId('prf-required.retry'));
    auth.prf = true;
    fireEvent.click(await screen.findByTestId('invite.create-passkey'));
    fireEvent.click(await screen.findByTestId('invite.unlock'));
    await expectJoined(harness, () => harness.app.userId!);
  });

  it('retries a failed accept and reports invite_registered', async () => {
    const harness = await openOrgInvite();
    fireEvent.change(await screen.findByTestId('invite.display-name'), { target: { value: 'Erin' } });
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    await screen.findByTestId('invite.unlock');
    const path = `POST /v1/orgs/${harness.setup.w.org}/log`;
    const append = harness.hub.override.get(path)!;
    harness.hub.override.set(path, new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('invite.unlock'));
    expect(await screen.findByTestId('invite.accept-failed')).toHaveAttribute('data-code', 'unavailable');
    harness.hub.override.set(path, new HubFailure(409, 'invite_registered'));
    fireEvent.click(screen.getByTestId('invite.accept-retry'));
    expect(await screen.findByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_registered');
    expect(append).toBeDefined();
  });

  it('retries after a failure and joins', async () => {
    const harness = await openOrgInvite();
    fireEvent.change(await screen.findByTestId('invite.display-name'), { target: { value: 'Erin' } });
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    await screen.findByTestId('invite.unlock');
    harness.hub.override.set('GET /v1/platform/log', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('invite.unlock'));
    expect(await screen.findByTestId('invite.accept-failed')).toHaveAttribute('data-code', 'unavailable');
    harness.hub.override.delete('GET /v1/platform/log');
    fireEvent.click(screen.getByTestId('invite.accept-retry'));
    await expectJoined(harness, () => harness.app.userId!);
  });
});

describe('OrgInvite: concurrent appends', () => {
  it('rebuilds the accept on a new lookup after seq_conflict', async () => {
    const harness = await openOrgInvite();
    fireEvent.change(await screen.findByTestId('invite.display-name'), { target: { value: 'Erin' } });
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    await screen.findByTestId('invite.unlock');
    const { w } = harness.setup;
    // the user is a member of another org (the membership check after the conflict looks through it)
    const other = uuid7();
    harness.hub.orgs.set(other, { id: other, defaultWg: uuid7(), owner: '', entries: [], wraps: [], events: [] });
    // an admin appends right before the accept arrives
    w.hub.before.push(() => {
      harness.hub.users.get(harness.app.userId!)!.orgs.push(other);
      return w.hub.foreign(w.me, { type: 'wg_create', wg: uuid7(), epoch: 1 });
    });
    fireEvent.click(screen.getByTestId('invite.unlock'));
    await expectJoined(harness, () => harness.app.userId!);
    expect(harness.hub.log.filter((entry) => entry.target === '/v1/invites/lookup')).toHaveLength(2);
  });
});

describe('OrgInvite: existing account', () => {
  it('joins directly with a signed-in, unlocked account', async () => {
    let userId = '';
    const harness = await openOrgInvite({}, async (made) => {
      userId = (await existingUser(made)).userId;
    });
    const existing = await screen.findByTestId('invite.existing');
    expect(existing).toHaveAttribute('data-mode', 'ready');
    expect(screen.getByTestId('invite.org-name')).toHaveTextContent('Join Grüße AG with your existing account');
    expect(screen.queryByTestId('invite.back')).toBeNull();
    fireEvent.click(screen.getByTestId('invite.join'));
    expect(screen.getByTestId('invite.display-name.invalid')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('invite.display-name'), { target: { value: 'Ada' } });
    fireEvent.click(screen.getByTestId('invite.join'));
    await expectJoined(harness, userId);
    // no second identity: the accept carries the registered one
    expect(harness.hub.users.size).toBe(1);
  });

  it('opens the org without an accept for a user who is already a member', async () => {
    const hub = new SimHub();
    const harness = await openOrgInvite({ hub }, async (made, setup) => {
      const registration = await existingUser(made);
      // the user joined earlier (by another invitation): the verified log lists it
      await setup.w.world.admitUser(setup.w.me, {
        name: 'me',
        id: registration.userId,
        identity: registration.identity,
      });
    });
    fireEvent.change(await screen.findByTestId('invite.display-name'), { target: { value: 'Ada' } });
    fireEvent.click(screen.getByTestId('invite.join'));
    await waitFor(() => expect(harness.router.state.location.pathname).toBe(`/o/${harness.setup.w.org}`));
    expect(
      harness.hub.log.some(
        (entry) => entry.target === `/v1/orgs/${harness.setup.w.org}/log` && entry.method === 'POST',
      ),
    ).toBe(false);
  });

  it('queues the display name when the package lacks the current org key', async () => {
    let userId = '';
    const harness = await openOrgInvite({}, async (made, setup) => {
      userId = (await existingUser(made)).userId;
      await setup.w.world.batch(setup.w.me, [{ type: 'rotate', scope: 'org', scope_id: setup.w.org, epoch: 2 }]);
    });
    fireEvent.change(await screen.findByTestId('invite.display-name'), { target: { value: 'Ada' } });
    fireEvent.click(screen.getByTestId('invite.join'));
    await waitFor(() => expect(harness.router.state.location.pathname).toBe(`/o/${harness.setup.w.org}`));
    expect(queuedSelfWraps(harness.app.store, harness.setup.w.org)).toMatchObject({ userId, profile: 'Ada' });
  });

  it('signs in from the intro with the passkey (after a cancelled attempt) and goes back on request', async () => {
    const auth = new FakeAuthenticator();
    let userId = '';
    const harness = await openOrgInvite({ auth }, async (made) => {
      userId = (await existingUser(made, { slot: true })).userId;
      await made.app.signOut();
    });
    fireEvent.click(await screen.findByTestId('invite.existing-account'));
    expect(screen.getByTestId('invite.existing')).toHaveAttribute('data-mode', 'sign_in');
    fireEvent.click(screen.getByTestId('invite.back'));
    fireEvent.click(await screen.findByTestId('invite.existing-account'));
    auth.refuse = 'NotAllowedError';
    fireEvent.click(screen.getByTestId('invite.sign-in'));
    expect(await screen.findByTestId('invite.notice')).toHaveTextContent('Sign-in was cancelled – try again.');
    harness.hub.override.set('POST /v1/passkeys/login/begin', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('invite.sign-in'));
    await waitFor(() => expect(screen.getByTestId('invite.notice')).toHaveTextContent('Service unavailable'));
    harness.hub.override.delete('POST /v1/passkeys/login/begin');
    harness.hub.override.set('GET /v1/me', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('invite.sign-in'));
    await waitFor(() => expect(screen.getByTestId('invite.notice')).toHaveTextContent('Service unavailable'));
    harness.hub.override.delete('GET /v1/me');
    fireEvent.click(screen.getByTestId('invite.sign-in'));
    await waitFor(() => expect(screen.getByTestId('invite.existing')).toHaveAttribute('data-mode', 'ready'));
    fireEvent.change(screen.getByTestId('invite.display-name'), { target: { value: 'Ada' } });
    fireEvent.click(screen.getByTestId('invite.join'));
    await expectJoined(harness, userId);
  });

  it('unlocks a locked session with the recovery code when the passkey cannot, and uploads the missing slot', async () => {
    let code = '';
    let userId = '';
    const harness = await openOrgInvite({}, async (made) => {
      const registration = await existingUser(made, { unlock: false });
      code = registration.recoveryCode;
      userId = registration.userId;
    });
    expect(await screen.findByTestId('invite.existing')).toHaveAttribute('data-mode', 'sign_in');
    fireEvent.click(screen.getByTestId('invite.sign-in'));
    expect(await screen.findByTestId('prf-missing.explain')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('prf-missing.recovery.input'), { target: { value: 'AAAAA' } });
    fireEvent.click(screen.getByTestId('prf-missing.recovery.submit'));
    expect(await screen.findByTestId('prf-missing.error')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('prf-missing.recovery.input'), { target: { value: code } });
    fireEvent.click(screen.getByTestId('prf-missing.recovery.submit'));
    await waitFor(() => expect(screen.getByTestId('invite.existing')).toHaveAttribute('data-mode', 'ready'));
    await waitFor(() => expect(harness.hub.users.get(userId)!.bundles.size).toBe(2));
    fireEvent.change(screen.getByTestId('invite.display-name'), { target: { value: 'Ada' } });
    fireEvent.click(screen.getByTestId('invite.join'));
    await expectJoined(harness, userId);
  });

  it('continues when the missing passkey slot cannot be uploaded', async () => {
    let code = '';
    const auth = new FakeAuthenticator();
    const harness = await openOrgInvite({ auth }, async (made) => {
      code = (await existingUser(made, { unlock: false })).recoveryCode;
    });
    fireEvent.click(await screen.findByTestId('invite.sign-in'));
    await screen.findByTestId('prf-missing.explain');
    const credential = b64uEncode(auth.credentials[0]!.id);
    harness.hub.override.set(`PUT /v1/me/bundles/passkey:${credential}`, new HubFailure(503, 'unavailable'));
    fireEvent.change(screen.getByTestId('prf-missing.recovery.input'), { target: { value: code } });
    fireEvent.click(screen.getByTestId('prf-missing.recovery.submit'));
    await waitFor(() => expect(screen.getByTestId('invite.existing')).toHaveAttribute('data-mode', 'ready'));
    expect([...harness.hub.users.values()][0]!.bundles.size).toBe(1);
  });

  it('falls back to the recovery code without PRF and for an identity that is not the registered one', async () => {
    const auth = new FakeAuthenticator();
    let code = '';
    const harness = await openOrgInvite({ auth }, async (made) => {
      code = (await existingUser(made, { slot: true })).recoveryCode;
      await made.app.signOut();
    });
    fireEvent.click(await screen.findByTestId('invite.existing-account'));
    // a passkey slot that does not open: the recovery code instead
    const user = [...harness.hub.users.values()][0]!;
    const [slot, bundle] = [...user.bundles].find(([name]) => name.startsWith('passkey:'))!;
    user.bundles.set(slot, b64uEncode(new Uint8Array(60)));
    fireEvent.click(screen.getByTestId('invite.sign-in'));
    expect(await screen.findByTestId('prf-missing.explain')).toBeInTheDocument();
    user.bundles.set(slot, bundle);
    fireEvent.click(screen.getByTestId('invite.back'));
    fireEvent.click(await screen.findByTestId('invite.existing-account'));
    // the hub registered other public keys than the slot holds: decrypt_failed → recovery code
    const me = harness.hub.override;
    me.set('GET /v1/me', () => ({ user_id: harness.app.userId, identity: { kem_pub: 'x', sig_pub: 'y' } }));
    fireEvent.click(screen.getByTestId('invite.sign-in'));
    expect(await screen.findByTestId('prf-missing.explain')).toBeInTheDocument();
    me.delete('GET /v1/me');
    fireEvent.click(screen.getByTestId('invite.back'));
    fireEvent.click(await screen.findByTestId('invite.existing-account'));
    auth.prf = false;
    fireEvent.click(screen.getByTestId('invite.sign-in'));
    expect(await screen.findByTestId('prf-missing.explain')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('prf-missing.recovery.input'), { target: { value: code } });
    fireEvent.click(screen.getByTestId('prf-missing.recovery.submit'));
    await waitFor(() => expect(screen.getByTestId('invite.existing')).toHaveAttribute('data-mode', 'ready'));
  });
});

describe('OrgInvite: refusals', () => {
  it('blocks a log with a forged signature and a package the code does not open', async () => {
    const hub = new SimHub();
    const setup = await orgWithInvite(hub);
    setup.patch = (raw) => {
      const log = raw.log as Entry[];
      log[1] = { ...log[1]!, sig: b64uEncode(new Uint8Array(64).fill(1)) };
    };
    window.history.replaceState({ key: 'invite' }, '', `/invite#${CODE}`);
    const first = await renderApp('/invite', { hub });
    expect(await screen.findByTestId('invite.blocked')).toHaveAttribute('data-code', 'log_signature');
    expect(screen.getByTestId('invite.blocked')).toHaveTextContent('This invitation cannot be trusted');
    first.view.unmount();
    setup.patch = (raw) => (raw.package_ct = 'A'.repeat(80));
    window.history.replaceState({ key: 'invite2' }, '', `/invite#${CODE}`);
    await renderApp('/invite', { hub });
    expect(await screen.findByTestId('invite.blocked')).toHaveAttribute('data-code', 'invite_code_invalid');
  });

  it('shows a revoked invitation as invalid', async () => {
    const hub = new SimHub();
    const setup = await orgWithInvite(hub);
    await setup.w.hub.foreign(setup.w.me, { type: 'invite_revoke', invite_id: setup.inviteId });
    window.history.replaceState({ key: 'invite' }, '', `/invite#${CODE}`);
    await renderApp('/invite', { hub });
    expect(await screen.findByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_unknown');
  });

  it('blocks a log that is not ASCII and stops a verification whose screen is gone', async () => {
    const hub = new SimHub();
    const setup = await orgWithInvite(hub);
    // a log entry that is not ASCII
    setup.patch = (raw) => {
      const log = raw.log as Entry[];
      log[1] = { ...log[1]!, body: { ...log[1]!.body, ts: 'ü' } };
    };
    window.history.replaceState({ key: 'invite' }, '', `/invite#${CODE}`);
    await renderApp('/invite', { hub });
    expect(await screen.findByTestId('invite.blocked')).toHaveAttribute('data-code', 'non_ascii');
    // a screen left while the verification runs (success and failure) sets nothing
    for (const patch of [null, (raw: Record<string, unknown>) => (raw.log = [])]) {
      setup.patch = patch;
      window.history.replaceState({ key: `gone ${String(patch)}` }, '', `/invite#${CODE}`);
      const gone = await renderApp('/invite', { hub });
      // unmount at the first tick that shows the verification (WEB-09: a 50 ms poll often came after its end)
      while (screen.queryAllByTestId('invite.verifying').length === 0)
        await act(() => new Promise((r) => setTimeout(r, 0)));
      gone.view.unmount();
      await act(() => new Promise((resolve) => setTimeout(resolve, 100)));
    }
    // a lookup that fails after the screen is gone sets nothing either (held until the unmount)
    const lookup = hub.override.get('POST /v1/invites/lookup');
    let fail: () => void = () => undefined;
    const held = new Promise<void>((resolve) => (fail = resolve));
    let asked = false;
    hub.override.set('POST /v1/invites/lookup', async () => {
      asked = true;
      await held;
      throw new HubFailure(404, 'invite_unknown');
    });
    window.history.replaceState({ key: 'gone lookup' }, '', `/invite#${CODE}`);
    const leaving = await renderApp('/invite', { hub });
    await waitFor(() => expect(asked).toBe(true));
    leaving.view.unmount();
    await act(async () => {
      fail();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    hub.override.set('POST /v1/invites/lookup', lookup!);
    expect(await codeToInviteId(CODE)).toBe(setup.inviteId);
  });
});

describe('OrgInvite: with the pairing of an agent (11-agent-access.md section 5.2)', () => {
  const PAIRING = '01a1ee56-5e00-7242-9e24-f87629ad2977';
  let submitted: HTMLFormElement[] = [];

  beforeEach(() => {
    submitted = [];
    vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(function (this: HTMLFormElement) {
      submitted.push(this);
    });
  });

  /** A hub that knows the open pairing of a fresh agent, and the fragment suffix of the agent's invitation. */
  async function agentHub(name = '&u=Erin') {
    const hub = new SimHub();
    const agent = await Identity.generate();
    hub.override.set(`GET /v1/agent-pairings/${PAIRING}`, () => ({
      pairing_id: PAIRING,
      pub: agent.public(),
      state: 'open',
      expires_at: formatTime(Date.now() + 600_000),
    }));
    hub.override.set('POST /v1/me/agents', () => ({ agent_id: PAIRING }));
    hub.override.set('GET /v1/me/agents', () => ({ agents: [] }));
    const fp = await fingerprint(agent.kemPublic(), agent.sigPublic());
    return { hub, suffix: `&p=${PAIRING}&f=${fp}&c=40123&a=Claude+Code&h=Mac${name}` };
  }

  const logins = (hub: SimHub) =>
    hub.log.filter((entry) => entry.target === '/v1/passkeys/login/begin').map((entry) => JSON.parse(entry.body));

  it('registers a new user with the proposed name, joins and connects the agent', async () => {
    const { hub, suffix } = await agentHub();
    const harness = await openOrgInvite({ hub, suffix });
    expect(await screen.findByTestId('invite.agent')).toBeInTheDocument();
    expect(screen.getByTestId('invite.proposed-name')).toHaveTextContent('Erin');
    expect(screen.queryByTestId('invite.display-name')).toBeNull();
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    fireEvent.click(await screen.findByTestId('invite.unlock'));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(harness.router.state.location.pathname).toBe('/connect');
    expect(accepted(harness.setup, harness.app.userId!)).toBe(true);
    expect(logins(hub)).toEqual([{ pairing_id: PAIRING }]);
  });

  it('signs an existing user in with the pairing; an expired request is dropped and the org opens', async () => {
    const auth = new FakeAuthenticator();
    const { hub, suffix } = await agentHub('');
    const harness = await openOrgInvite({ hub, auth, suffix }, async (made) => {
      await existingUser(made, { slot: true });
      await made.app.signOut();
    });
    fireEvent.click(await screen.findByTestId('invite.existing-account'));
    expect(screen.getByTestId('invite.agent')).toBeInTheDocument();
    hub.override.set('POST /v1/passkeys/login/begin', new HubFailure(404, 'pairing_unknown'));
    fireEvent.click(screen.getByTestId('invite.sign-in'));
    expect(await screen.findByTestId('invite.notice')).toHaveTextContent('Connection request expired');
    expect(screen.queryByTestId('invite.agent')).toBeNull();
    hub.override.delete('POST /v1/passkeys/login/begin');
    fireEvent.click(screen.getByTestId('invite.sign-in'));
    await waitFor(() => expect(screen.getByTestId('invite.existing')).toHaveAttribute('data-mode', 'ready'));
    fireEvent.change(screen.getByTestId('invite.display-name'), { target: { value: 'Ada' } });
    fireEvent.click(screen.getByTestId('invite.join'));
    await waitFor(() => expect(harness.router.state.location.pathname).toBe(`/o/${harness.setup.w.org}`));
    expect(logins(hub).at(-1)).toEqual({});
    expect(submitted).toHaveLength(0);
  });

  it('signs an existing user in with the pairing and connects the agent after the join', async () => {
    const { hub, suffix } = await agentHub();
    const harness = await openOrgInvite({ hub, suffix }, async (made) => {
      await existingUser(made, { slot: true });
      await made.app.signOut();
    });
    fireEvent.click(await screen.findByTestId('invite.existing-account'));
    fireEvent.click(screen.getByTestId('invite.sign-in'));
    await waitFor(() => expect(screen.getByTestId('invite.existing')).toHaveAttribute('data-mode', 'ready'));
    expect(screen.getByTestId('invite.proposed-name')).toHaveTextContent('Erin');
    fireEvent.click(screen.getByTestId('invite.join'));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(harness.router.state.location.pathname).toBe('/connect');
    expect(logins(hub).at(-1)).toEqual({ pairing_id: PAIRING });
  });
});
