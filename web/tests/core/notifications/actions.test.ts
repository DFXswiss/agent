// Notification actions against the scriptable client: list pages, malformed answers, marking read (one, all) after
// the hub accepted it, and the name pulls for the texts (profiles once per org, device profiles for accepted
// invitations, task titles only in readable workgroups, nothing for orgs without a verified log).
import { describe, expect, it } from 'vitest';
import { loadNotifications, pullNames, readAll, readNotification } from '../../../src/core/notifications/actions';
import { HubError } from '../../../src/core/net/signedFetch';
import { selectUnread } from '../../../src/core/store/slices/notifications';
import { setProfile } from '../../../src/core/store/slices/me';
import { boardWorld } from '../whiteboards/world';
import { notification } from './rows';

const TASK = '0190f000-0000-7000-8000-00000000000c';
const empty = { events: [], next_after: null, head: 0 };

describe('loadNotifications', () => {
  it('loads pages newest first and stops without a cursor', async () => {
    const w = await boardWorld();
    w.client.on('GET /v1/notifications', (_body, target) =>
      target.includes('before=')
        ? { notifications: [notification('n1')], next_before: null }
        : { notifications: [notification('n2')], next_before: 'c' },
    );
    await loadNotifications(w.context);
    expect(w.store.getState().notifications.list).toEqual({ status: 'ready', code: null, nextBefore: 'c' });
    await loadNotifications(w.context, true);
    expect(w.client.of('GET /v1/notifications')[1]!.target).toBe('/v1/notifications?account=1&limit=50&before=c');
    expect(Object.keys(w.store.getState().notifications.rows).sort()).toEqual(['n1', 'n2']);
    await loadNotifications(w.context, true);
    expect(w.client.of('GET /v1/notifications')).toHaveLength(2);
  });

  it('records malformed answers and failures', async () => {
    const w = await boardWorld();
    w.client.on('GET /v1/notifications', () => ({ notifications: [{}], next_before: null }));
    await loadNotifications(w.context);
    expect(w.store.getState().notifications.list).toMatchObject({ status: 'error', code: 'unavailable' });
    w.client.on('GET /v1/notifications', () => null);
    await loadNotifications(w.context);
    expect(w.store.getState().notifications.list.code).toBe('unavailable');
    w.client.on('GET /v1/notifications', () => {
      throw new HubError('rate_limited', 429);
    });
    await loadNotifications(w.context);
    expect(w.store.getState().notifications.list.code).toBe('rate_limited');
  });
});

describe('marking read', () => {
  it('marks one and all after the hub accepted it', async () => {
    const w = await boardWorld();
    setProfile(w.store, { unread_notifications: 2 });
    const bodies: unknown[] = [];
    w.client.on('POST /v1/notifications/read', (body) => {
      bodies.push(body);
      return {};
    });
    w.client.on('GET /v1/notifications', () => ({
      notifications: [notification('n1'), notification('n2')],
      next_before: null,
    }));
    await loadNotifications(w.context);
    await readNotification(w.context, 'n1');
    expect(selectUnread(w.store.getState())).toBe(1);
    expect(w.store.getState().notifications.rows.n1!.read_at).toBe('2026-11-21T12:00:00.000Z');
    await readAll(w.context);
    expect(selectUnread(w.store.getState())).toBe(0);
    expect(bodies).toEqual([{ ids: ['n1'] }, { all: true }]);
  });

  it('changes nothing when the hub refuses', async () => {
    const w = await boardWorld();
    w.client.on('GET /v1/notifications', () => ({ notifications: [notification('n1')], next_before: null }));
    await loadNotifications(w.context);
    await expect(readNotification(w.context, 'n1')).rejects.toMatchObject({ code: 'not_found' });
    expect(w.store.getState().notifications.rows.n1!.read_at).toBeNull();
  });
});

describe('pullNames', () => {
  it('pulls profiles, device profiles and readable task titles', async () => {
    const w = await boardWorld({ member: true });
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => empty);
    w.client.on(`GET /v1/scopes/${w.wg}/events`, () => empty);
    const rows = [
      notification('n1', { org_id: w.org }),
      notification('n2', { org_id: w.org, kind: 'invite.accepted' }),
      notification('n3', { org_id: w.org, kind: 'task.idle', wg_id: w.wg, task_id: TASK }),
      // a workgroup bob does not read and a task without workgroup: no title pull
      notification('n4', {
        org_id: w.org,
        kind: 'task.idle',
        wg_id: '0190f000-0000-7000-8000-0000000000ff',
        task_id: TASK,
      }),
      notification('n5', { org_id: w.org, kind: 'task.failed', task_id: TASK }),
      // an org without a verified log
      notification('n6', { org_id: '0190f000-0000-7000-8000-0000000000aa' }),
    ];
    await pullNames(w.context, rows);
    const kinds = w.client.calls.map((call) => new URL(call.target, 'https://h').searchParams.get('kinds'));
    expect(kinds.sort()).toEqual(['device.profile', 'org.profile,wg.profile,member.profile', 'task.meta']);
  });

  it('loads the agents once for account-level rows and skips their missing org', async () => {
    const w = await boardWorld();
    w.client.on('GET /v1/me/agents', () => ({ agents: [] }));
    const row = notification('n1', { kind: 'agent.account_change', org_id: null, ref: 'agent_revoked:a:' });
    await pullNames(w.context, [row]);
    expect(w.client.of('GET /v1/me/agents')).toHaveLength(1);
    await pullNames(w.context, [row]);
    expect(w.client.of('GET /v1/me/agents')).toHaveLength(1);
    w.store.update('agents', () => ({ entries: null, status: 'idle', code: null }));
    w.store.update('me', (me) => ({ ...me, identity: null }));
    await pullNames(w.context, [row]);
    expect(w.client.calls).toHaveLength(1);
  });

  it('ignores failed pulls and needs a signed-in user', async () => {
    const w = await boardWorld();
    await pullNames(w.context, [notification('n1', { org_id: w.org })]);
    expect(w.client.calls).toHaveLength(1);
    w.store.update('me', (me) => ({ ...me, userId: null }));
    await pullNames(w.context, [notification('n1', { org_id: w.org })]);
    expect(w.client.calls).toHaveLength(1);
  });
});

describe('after a sign-out', () => {
  it('drops list answers and read confirmations', async () => {
    const w = await boardWorld();
    w.client.on('GET /v1/notifications', () => {
      w.store.reset();
      return { notifications: [notification('n1')], next_before: null };
    });
    await loadNotifications(w.context);
    expect(w.store.getState().notifications.rows).toEqual({});
    w.client.on('GET /v1/notifications', () => {
      w.store.reset();
      throw new HubError('unavailable', 503);
    });
    await loadNotifications(w.context);
    expect(w.store.getState().notifications.list.status).toBe('idle');
    w.client.on('GET /v1/notifications', () => ({ notifications: [notification('n1')], next_before: null }));
    await loadNotifications(w.context);
    w.client.on('POST /v1/notifications/read', () => {
      w.store.reset();
      return {};
    });
    await readNotification(w.context, 'n1');
    await readAll(w.context);
    expect(w.store.getState().notifications.rows).toEqual({});
    expect(w.store.getState().notifications.unread).toBeNull();
  });
});
