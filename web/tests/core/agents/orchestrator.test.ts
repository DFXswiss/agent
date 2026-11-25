// A session on DFX hardware as the main orchestrator (11-agent-access.md section 11, ADR-082 sections 6 and 9.8): the
// hub's view is parsed strictly; the page waits for the session's keys; the keys must match the fingerprint and the
// device's signature must verify under the device key of the verified platform log (a forged or foreign key, an
// uncertified or decertified device, another session or pairing: `pairing_mismatch`); the handover is sealed to the
// session's key and signed by the user over the ciphertext.
import { describe, expect, it, vi } from 'vitest';
import {
  checkSessionPairing,
  handOver,
  loadOrchestrator,
  type OrchestratorView,
  parseView,
  privateOf,
  POLL_MS,
  requestOrchestrator,
  sessionLabel,
  type SessionPairing,
  WAIT_MS,
  waitForPairing,
} from '../../../src/core/agents/orchestrator';
import type { PreparedPairing } from '../../../src/core/agents/pairing';
import {
  checkLabel,
  fingerprint,
  openHandover,
  orchestratorHandoverText,
  orchestratorKeyText,
  signText,
  verifyText,
} from '../../../src/core/crypto/agent';
import { Identity } from '../../../src/core/crypto/keys';
import { Secret } from '../../../src/core/crypto/secret';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import type { PlatformState } from '../../../src/core/log/platform';
import type { OrgState } from '../../../src/core/log/state';
import { actors, World } from '../log/world';
import { HubError } from '../../../src/core/net/signedFetch';
import { AGENT, OTHER, USER } from './world';

const SESSION = '01a1ee56-5e00-7d00-8e24-f87629ad2970';
const DEVICE = '01a1ee56-5e00-7d00-8e24-f87629ad2971';
const EXPIRES = '2026-11-25T10:10:00.000Z';
const REQUESTED = '2026-11-25T10:00:00.000Z';
const ROOT = b64uEncode(new Uint8Array(32).fill(7));

async function scene() {
  const agent = await Identity.generate();
  const device = await Identity.generate();
  const fp = await fingerprint(agent.kemPublic(), agent.sigPublic());
  const deviceSig = await signText(device.sig.privateKey, orchestratorKeyText(fp, AGENT, SESSION));
  const pairing = {
    pairing_id: AGENT,
    pub: agent.public(),
    fp,
    device_sig: deviceSig,
    state: 'open',
    expires_at: EXPIRES,
    handover: false,
  };
  const body = {
    session_id: SESSION,
    device_id: DEVICE,
    available: true,
    reason: null,
    requested_at: REQUESTED,
    pairing,
  };
  return { agent, device, fp, deviceSig, pairing, body };
}

/** A verified platform state in which `device` holds the certification `seq` (decertified when asked). */
function platform(device: Identity, decertified = false): PlatformState {
  const certification = { device: DEVICE, kem: device.public().kem, sig: device.public().sig, seq: 3 };
  return {
    devices: new Map([[DEVICE, 3]]),
    certification: (seq: number) =>
      seq === 3 ? { ...certification, inviteId: 'i', decertifiedSeq: decertified ? 5 : null } : null,
  } as unknown as PlatformState;
}

const codeOf = async (run: () => Promise<unknown>) => {
  try {
    await run();
  } catch (error) {
    return error instanceof HubError ? error.code : (error as { code?: string }).code;
  }
  return 'none';
};

describe('parseView', () => {
  it('reads the view of the session and its pairing', async () => {
    const { body, agent } = await scene();
    const view = parseView(body, SESSION);
    expect(view).toMatchObject({ sessionId: SESSION, deviceId: DEVICE, available: true, reason: null });
    expect(view.requestedAt).toBe(Date.parse(REQUESTED));
    expect(view.pairing).toMatchObject({ pairingId: AGENT, pub: agent.public(), state: 'open', handover: false });
    expect(view.pairing!.expiresAt).toBe(Date.parse(EXPIRES));
    const none = parseView(
      { ...body, device_id: null, available: false, reason: 'not_private', requested_at: null, pairing: null },
      SESSION,
    );
    expect(none).toEqual({
      sessionId: SESSION,
      deviceId: null,
      available: false,
      reason: 'not_private',
      requestedAt: null,
      pairing: null,
    });
  });

  it('refuses a malformed answer', async () => {
    const { body, pairing } = await scene();
    const broken: unknown[] = [
      null,
      { ...body, session_id: OTHER },
      { ...body, available: 'yes' },
      { ...body, device_id: 7 },
      { ...body, reason: 7 },
      { ...body, pairing: 'x' },
      { ...body, pairing: { ...pairing, pub: null } },
      { ...body, pairing: { ...pairing, state: 'revoked' } },
      { ...body, pairing: { ...pairing, handover: 'no' } },
      { ...body, pairing: { ...pairing, pairing_id: 7 } },
      { ...body, pairing: { ...pairing, fp: 7 } },
      { ...body, pairing: { ...pairing, device_sig: 7 } },
      { ...body, pairing: { ...pairing, pub: { kem: 7, sig: 'x' } } },
      { ...body, pairing: { ...pairing, pub: { kem: 'x', sig: 7 } } },
    ];
    for (const value of broken) expect(await codeOf(async () => parseView(value, SESSION))).toBe('header_invalid');
  });
});

describe('loadOrchestrator and requestOrchestrator', () => {
  it('read and ask at the session path', async () => {
    const { body } = await scene();
    const request = vi.fn(async () => body);
    await loadOrchestrator({ request }, SESSION);
    await requestOrchestrator({ request }, SESSION);
    expect(request.mock.calls).toEqual([
      ['GET', `/v1/sessions/${SESSION}/orchestrator`],
      ['POST', `/v1/sessions/${SESSION}/orchestrator`, { body: {} }],
    ]);
  });
});

describe('waitForPairing', () => {
  const view = (pairing: SessionPairing | null, available = true): OrchestratorView => ({
    sessionId: SESSION,
    deviceId: DEVICE,
    available,
    reason: available ? null : 'not_running',
    requestedAt: 0,
    pairing,
  });

  it('reads the view until the open pairing is there', async () => {
    const { body } = await scene();
    const answers = [{ ...body, pairing: null }, body];
    const request = vi.fn(async () => answers.shift());
    const slept: number[] = [];
    const found = await waitForPairing({ request }, view(null), {
      now: () => 0,
      sleep: async (ms) => void slept.push(ms),
    });
    expect(found.pairing?.state).toBe('open');
    expect(slept).toEqual([POLL_MS, POLL_MS]);
    // an open pairing in the first answer needs no read
    expect(await waitForPairing({ request }, found, { now: () => 0, sleep: async () => {} })).toBe(found);
  });

  it('stops when the session is refused, after the wait or when the page closed', async () => {
    const request = vi.fn(async () => ({}));
    const refused = await codeOf(() =>
      waitForPairing({ request }, view(null, false), { now: () => 0, sleep: vi.fn() }),
    );
    expect(refused).toBe('orchestrator_unavailable');
    let time = 0;
    const late = await codeOf(() =>
      waitForPairing({ request }, view(null), { now: () => (time += WAIT_MS), sleep: vi.fn() }),
    );
    expect(late).toBe('unavailable');
    const closed = await codeOf(() =>
      waitForPairing({ request }, view(null), { now: () => 0, sleep: vi.fn(), live: () => false }),
    );
    expect(closed).toBe('unavailable');
    expect(request).not.toHaveBeenCalled();
  });
});

/** A verified org state as `privateOf` reads it. */
function org(options: { private?: boolean; founder?: string; users?: [string, number | null][] } = {}): OrgState {
  const users = options.users ?? [[OWNER, null]];
  const principals = new Map(
    [...users, [DEVICE, null] as [string, number | null]].map(([id, removedSeq]) => [
      id,
      { id, kind: id === DEVICE ? 'device' : 'user', sig: `sig-${id}`, removedSeq },
    ]),
  );
  return {
    private: options.private ?? true,
    founder: options.founder ?? `sig-${OWNER}`,
    principals,
    member: (id: string) => {
      const found = principals.get(id);
      return found !== undefined && found.removedSeq === null ? found : null;
    },
  } as unknown as OrgState;
}

const OWNER = USER;
const ORG = org();

describe('privateOf', () => {
  it("is true only for the owner's private organization with no other user member", async () => {
    expect(privateOf(ORG, OWNER)).toBe(true);
    expect(
      privateOf(
        org({
          users: [
            [OWNER, null],
            [OTHER, 4],
          ],
        }),
        OWNER,
      ),
    ).toBe(true);
    expect(privateOf(null, OWNER)).toBe(false);
    expect(privateOf(org({ private: false }), OWNER)).toBe(false);
    expect(privateOf(org({ founder: `sig-${OTHER}` }), OWNER)).toBe(false);
    expect(
      privateOf(
        org({
          users: [
            [OWNER, null],
            [OTHER, null],
          ],
        }),
        OWNER,
      ),
    ).toBe(false);
    expect(privateOf(org({ users: [[OWNER, 3]] }), OWNER)).toBe(false);
    // a real verified log: a private org founded by the owner, then with a second member
    const [ada, bob] = await actors('ada', 'bob');
    const world = await World.create(ada, { private: true });
    expect(privateOf(world.state, ada.id)).toBe(true);
    expect(privateOf(world.state, bob.id)).toBe(false);
    await world.admitUser(ada, bob);
    expect(privateOf(world.state, ada.id)).toBe(false);
  });
});

describe('checkSessionPairing', () => {
  it('refuses an organization that is not the private one of the owner', async () => {
    const { body, device } = await scene();
    const view = parseView(body, SESSION);
    const refused = await codeOf(() =>
      checkSessionPairing(view, platform(device), view.pairing!, org({ private: false }), OWNER),
    );
    expect(refused).toBe('orchestrator_unavailable');
  });

  it('accepts the keys the session device signed', async () => {
    const { body, device, agent } = await scene();
    const view = parseView(body, SESSION);
    const pairing = await checkSessionPairing(view, platform(device), view.pairing!, ORG, OWNER);
    expect(pairing).toEqual({
      pairingId: AGENT,
      kemPub: agent.kemPublic(),
      sigPub: agent.sigPublic(),
      expiresAt: Date.parse(EXPIRES),
      requestedAt: Date.parse(REQUESTED),
      remote: false,
      agentCode: null,
    });
    const unrequested = { ...view, requestedAt: null };
    expect((await checkSessionPairing(unrequested, platform(device), view.pairing!, ORG, OWNER)).requestedAt).toBe(
      Date.parse(EXPIRES),
    );
  });

  it('refuses keys or devices the hub swapped', async () => {
    const { body, device, deviceSig, pairing } = await scene();
    const other = await Identity.generate();
    const view = parseView(body, SESSION);
    const check = (changes: Partial<OrchestratorView>, state: PlatformState | null, value = view.pairing!) =>
      codeOf(() => checkSessionPairing({ ...view, ...changes }, state, value, ORG, OWNER));
    // no verified platform log, no device, an uncertified or a decertified device
    expect(await check({}, null)).toBe('pairing_mismatch');
    expect(await check({ deviceId: null }, platform(device))).toBe('pairing_mismatch');
    expect(await check({ deviceId: OTHER }, platform(device))).toBe('pairing_mismatch');
    expect(await check({}, platform(device, true))).toBe('pairing_mismatch');
    // the keys do not have the fingerprint; the signature is another device's or over another session or pairing
    const swapped = parseView({ ...body, pairing: { ...pairing, pub: other.public() } }, SESSION).pairing!;
    expect(await check({}, platform(device), swapped)).toBe('pairing_mismatch');
    expect(await check({}, platform(other), view.pairing!)).toBe('pairing_mismatch');
    const foreign = await signText(device.sig.privateKey, orchestratorKeyText(pairing.fp, AGENT, OTHER));
    expect(await check({}, platform(device), { ...view.pairing!, deviceSig: foreign })).toBe('pairing_mismatch');
    expect(await check({}, platform(device), { ...view.pairing!, deviceSig: deviceSig.slice(2) })).toBe(
      'pairing_mismatch',
    );
    expect(await check({}, platform(device), { ...view.pairing!, pairingId: OTHER })).toBe('pairing_mismatch');
  });
});

describe('sessionLabel', () => {
  it('names the agent and its device under the label rules', () => {
    const label = sessionLabel('Mac mini 3', 'A session on DFX hardware');
    expect(checkLabel({ host: label.host, name: label.name })).toEqual({
      host: 'Mac mini 3',
      name: 'A session on DFX hardware',
    });
    expect(sessionLabel(null, 'x').host).toBe('DFX');
    expect(sessionLabel('a\u202eb', 'x').host).toBe('DFX');
    expect([...sessionLabel('x'.repeat(80), 'x').host]).toHaveLength(64);
  });
});

describe('handOver', () => {
  it('seals r and the root key to the session and signs the ciphertext as the user', async () => {
    const { agent } = await scene();
    const user = await Identity.generate();
    const secret = Secret.of(new Uint8Array(32).fill(3));
    const prepared = { pairingId: AGENT, userId: USER, secret } as unknown as PreparedPairing;
    const pairing = {
      pairingId: AGENT,
      kemPub: agent.kemPublic(),
      sigPub: agent.sigPublic(),
      expiresAt: 0,
      requestedAt: 0,
      remote: false,
      agentCode: null,
    };
    const sent: { ct: string; sig: string }[] = [];
    const request = vi.fn(async (_m: string, _t: string, options: { body: { ct: string; sig: string } }) => {
      sent.push(options.body);
      return { pairing_id: AGENT, session_id: SESSION };
    });
    await handOver({ request } as never, prepared, pairing, SESSION, user, ROOT);
    expect(request.mock.calls[0]![0]).toBe('POST');
    expect(request.mock.calls[0]![1]).toBe(`/v1/agent-pairings/${AGENT}/orchestrator-handover`);
    const { ct, sig } = sent[0]!;
    await verifyText(user.sigPublic(), sig, orchestratorHandoverText(ct, AGENT, SESSION));
    const opened = await openHandover(agent.kem, AGENT, USER, ct);
    expect(b64uEncode(opened.secret)).toBe(b64uEncode(new Uint8Array(32).fill(3)));
    expect(opened.rootKey).toBe(ROOT);
    expect(b64uDecode(sig)).toHaveLength(64);
    const wrong = vi.fn(async () => ({ pairing_id: AGENT, session_id: OTHER }));
    expect(await codeOf(() => handOver({ request: wrong }, prepared, pairing, SESSION, user, ROOT))).toBe(
      'header_invalid',
    );
  });
});
