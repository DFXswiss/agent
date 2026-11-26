// Who is DFX support and how it is named (ADR-096 sections 1 and 5): roster and support authors from the verified
// platform log only, once per state; the operator's names from `GET /v1/orgs/{org}/support`; the ID marks.
import { describe, expect, it } from 'vitest';
import { senderMark } from '../../../src/core/community/mark';
import { HubError } from '../../../src/core/net/signedFetch';
import { NO_ROSTER } from '../../../src/core/log/state';
import { authorsOf, ensureMarks, isSupportPerson, rosterOf, supportName } from '../../../src/core/support/names';
import { loadOrgSupport } from '../../../src/core/support/people';
import { pullNames } from '../../../src/core/notifications/actions';
import { notification } from '../notifications/rows';
import { orgWorld } from '../org/world';
import { supportWorld } from './world';

describe('roster and authors', () => {
  it('come from the verified platform state, once per state', async () => {
    const s = await supportWorld({ former: true });
    const platform = s.store.getState().orgs.platform!.state;
    expect([...rosterOf(platform).keys()]).toEqual([s.a.tom.id]);
    expect([...authorsOf(platform).keys()].sort()).toEqual([s.a.sam.id, s.a.tom.id].sort());
    expect(rosterOf(platform)).toBe(rosterOf(platform));
    expect(authorsOf(platform)).toBe(authorsOf(platform));
    expect(rosterOf(null)).toBe(NO_ROSTER);
    expect(authorsOf(null)).toBe(NO_ROSTER);
    // sam left support: no support area, but still named as a support author
    expect(isSupportPerson(s.store.getState())).toBe(false);
    expect(supportName(s.store.getState(), s.w.org, s.a.sam.id)).toEqual({ name: null, mark: null });
    expect(supportName(s.store.getState(), s.w.org, s.a.ada.id)).toBeNull();
  });

  it('knows the signed-in person of the roster', async () => {
    const s = await supportWorld();
    expect(isSupportPerson(s.store.getState())).toBe(true);
    s.store.reset();
    expect(isSupportPerson(s.store.getState())).toBe(false);
  });
});

describe('names and marks', () => {
  it('loads the releases and the persons of an org, and keeps them over a failure', async () => {
    const w = await orgWorld({ support: ['sam'] });
    const sam = w.others.sam.id;
    const release = { wg_id: 'w', access: 'write', granted_seq: 4, granted_at: '2026-11-21T12:00:00.000Z' };
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => ({
      releases: [release],
      people: [{ user_id: sam, name: 'Sam' }],
    }));
    const loading = loadOrgSupport(w.context, w.org);
    expect(w.store.getState().support.orgs[w.org]!.status).toBe('loading');
    await loading;
    expect(w.store.getState().support.orgs[w.org]).toEqual({
      status: 'ready',
      code: null,
      releases: [release],
      people: { [sam]: 'Sam' },
    });
    await ensureMarks(w.store, [sam, sam]);
    expect(supportName(w.store.getState(), w.org, sam)).toEqual({ name: 'Sam', mark: await senderMark(sam) });
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => ({ releases: [], people: [{ user_id: sam }] }));
    await loadOrgSupport(w.context, w.org);
    expect(w.store.getState().support.orgs[w.org]).toMatchObject({
      status: 'error',
      code: 'unavailable',
      people: { [sam]: 'Sam' },
    });
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => ({ releases: [{ ...release, access: 'admin' }], people: [] }));
    await loadOrgSupport(w.context, w.org);
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => ({ releases: 'none', people: [] }));
    await loadOrgSupport(w.context, w.org);
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => ({ releases: [], people: 'none' }));
    await loadOrgSupport(w.context, w.org);
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => {
      throw new HubError('forbidden', 403);
    });
    await loadOrgSupport(w.context, w.org);
    expect(w.store.getState().support.orgs[w.org]).toMatchObject({ status: 'error', code: 'forbidden' });
  });

  it('starts empty, accepts unnamed persons and writes nothing after a sign-out', async () => {
    const w = await orgWorld({ support: ['sam'] });
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => ({ releases: [], people: [{ user_id: 'p', name: null }] }));
    await loadOrgSupport(w.context, w.org);
    expect(w.store.getState().support.orgs[w.org]!.people).toEqual({ p: null });
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => {
      w.store.reset();
      return { releases: [], people: [] };
    });
    await loadOrgSupport(w.context, w.org);
    expect(w.store.getState().support.orgs).toEqual({});
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => {
      w.store.reset();
      throw new HubError('unavailable', 503);
    });
    await loadOrgSupport(w.context, w.org);
    expect(w.store.getState().support.orgs).toEqual({});
    // marks: nothing to do, and none written after a sign-out
    await ensureMarks(w.store, []);
    const marks = ensureMarks(w.store, ['x']);
    w.store.reset();
    await marks;
    expect(w.store.getState().support.marks).toEqual({});
  });
});

describe('notification names', () => {
  it('loads the persons of an org for a first access once', async () => {
    const w = await orgWorld({ support: ['sam'] });
    const sam = w.others.sam.id;
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => ({ releases: [], people: [{ user_id: sam, name: 'Sam' }] }));
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => ({ events: [], next_before: null, head: 0 }));
    const row = notification('n1', { kind: 'support.access', org_id: w.org, ref: sam });
    await pullNames(w.context, [row]);
    await pullNames(w.context, [row]);
    expect(w.client.of(`GET /v1/orgs/${w.org}/support`)).toHaveLength(1);
    expect(w.store.getState().support.orgs[w.org]!.people).toEqual({ [sam]: 'Sam' });
  });
});
