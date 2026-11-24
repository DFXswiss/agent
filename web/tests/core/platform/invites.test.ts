// U-WEB-ui-platform-invites (core part): the inviter right from the verified platform log, the code drawn in the
// browser (only the ID and the expiry reach the hub), the list and the revocation.
import { describe, expect, it } from 'vitest';
import { formatCode, inviteId, inviteSecrets } from '../../../src/core/crypto/invite';
import { formatTime } from '../../../src/core/encoding/time';
import { actionErrorCode } from '../../../src/core/org/batch';
import {
  createPlatformInvite,
  EXPIRY_HOURS,
  isInviter,
  OPEN_INVITES,
  listPlatformInvites,
  type PlatformInviteRow,
  preparePlatformInvite,
  revokePlatformInvite,
  USER_EXPIRY_HOURS,
} from '../../../src/core/platform/invites';
import { pairKey } from '../../../src/core/log/state';
import { platformWith, T0 } from '../log/world';
import { FakeClient } from '../tasks/world';

const CODE = new Uint8Array(16).fill(7);

async function refusal(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (error) {
    return actionErrorCode(error);
  }
  throw new Error('not refused');
}

const row = (extra: Partial<PlatformInviteRow> = {}): PlatformInviteRow => ({
  invite_id: 'id',
  kind: 'user',
  state: 'open',
  created_by: null,
  created_at: '2026-11-21T12:00:00.000Z',
  expires_at: '2026-11-24T12:00:00.000Z',
  registered: false,
  used_at: null,
  revoked_at: null,
  ...extra,
});

describe('the inviter right', () => {
  it('comes from the verified platform log only', async () => {
    const { state } = await platformWith([]);
    expect(isInviter(null, 'u')).toBe(false);
    expect(isInviter(state, null)).toBe(false);
    expect(isInviter(state, 'u')).toBe(false);
    const granted = state.with({ roles: new Map([[pairKey('u', 'inviter'), 3]]) });
    expect(isInviter(granted, 'u')).toBe(true);
    // ADR-079: while the switch `open_platform_invites` is on, every user invites; off again, only the role counts
    const open = state.with({ policies: new Map([[OPEN_INVITES, true]]) });
    expect(isInviter(open, 'u')).toBe(true);
    expect(isInviter(open, null)).toBe(false);
    expect(isInviter(state.with({ policies: new Map([[OPEN_INVITES, false]]) }), 'u')).toBe(false);
  });
});

describe('creating an invitation', () => {
  it('draws the code once and sends only the ID, the invitation key and the expiry', async () => {
    expect(USER_EXPIRY_HOURS).toBe(72);
    expect(EXPIRY_HOURS).toContain(720);
    const prepared = await preparePlatformInvite({
      hours: 72,
      now: T0,
      origin: 'https://hub.example',
      code: () => CODE,
    });
    expect(prepared).toEqual({
      inviteId: await inviteId(CODE),
      pkInv: (await inviteSecrets(CODE)).pkInv,
      code: formatCode(CODE),
      link: `https://hub.example/invite#${formatCode(CODE)}`,
      expires: formatTime(T0 + 72 * 3600_000),
    });
    const client = new FakeClient().on('POST /v1/platform/invites', () => ({}));
    await createPlatformInvite(client, prepared);
    expect(client.calls[0]!.body).toEqual({
      invite_id: prepared.inviteId,
      pk_inv: prepared.pkInv,
      kind: 'user',
      expires_at: prepared.expires,
    });
    expect(JSON.stringify(client.calls)).not.toContain(prepared.code);
    // the longest choice keeps a margin for the clock skew between browser and hub
    const longest = await preparePlatformInvite({ hours: 720, now: T0, origin: 'o', code: () => CODE });
    expect(longest.expires).toBe(formatTime(T0 + 720 * 3600_000 - 120_000));
    const random = await preparePlatformInvite({ hours: 1, now: T0, origin: 'https://hub.example' });
    expect(random.code).not.toBe(prepared.code);
  });

  it('refuses expiries outside 1 h – 30 d and malformed answers', async () => {
    for (const hours of [0, 721, 1.5]) {
      expect(await refusal(preparePlatformInvite({ hours, now: T0, origin: 'o' }))).toBe('params_invalid');
    }
    const prepared = await preparePlatformInvite({ hours: 1, now: T0, origin: 'o', code: () => CODE });
    const client = new FakeClient().on('POST /v1/platform/invites', () => null);
    expect(await refusal(createPlatformInvite(client, prepared))).toBe('unavailable');
  });
});

describe('the list and revocation', () => {
  it('reads valid lists and refuses malformed ones', async () => {
    const rows = [row(), row({ invite_id: 'b', state: 'used', created_by: 'u', used_at: 't', registered: true })];
    const client = new FakeClient().on('GET /v1/platform/invites', () => ({ invites: rows }));
    expect(await listPlatformInvites(client)).toEqual(rows);
    const bad: unknown[] = [
      null,
      { invites: 'x' },
      { invites: [{ ...row(), kind: 'device' }] },
      { invites: [{ ...row(), state: 'gone' }] },
      { invites: [{ ...row(), invite_id: 1 }] },
      { invites: [{ ...row(), created_by: 1 }] },
      { invites: [{ ...row(), created_at: null }] },
      { invites: [{ ...row(), expires_at: null }] },
      { invites: [{ ...row(), registered: 'no' }] },
      { invites: [{ ...row(), used_at: 1 }] },
      { invites: [{ ...row(), revoked_at: 1 }] },
    ];
    for (const body of bad) {
      client.on('GET /v1/platform/invites', () => body);
      expect(await refusal(listPlatformInvites(client))).toBe('unavailable');
    }
  });

  it('revokes by ID', async () => {
    const client = new FakeClient().on('POST /v1/platform/invites/a%2Fb/revoke', () => ({}));
    await revokePlatformInvite(client, 'a/b');
    expect(client.calls[0]).toMatchObject({ method: 'POST', body: {} });
  });
});
