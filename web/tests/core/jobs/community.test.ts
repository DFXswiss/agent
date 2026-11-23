// The background job `community` (12-community.md section 6): registered after the org jobs, it starts the community
// client of the sign-in on `login` without waiting for it and ticks it on `interval`.
import { describe, expect, it } from 'vitest';
import { Prefs } from '../../../src/core/account/prefs';
import { communityOf } from '../../../src/core/community/engine';
import { backgroundJobs, type JobContext } from '../../../src/core/jobs';
import { job } from '../../../src/core/jobs/community';
import { CommunityHub, until, type User } from '../community/hub';

const context = (hub: CommunityHub, user: User, prefs?: Prefs): JobContext => ({
  store: user.store,
  client: user.client,
  persist: {} as JobContext['persist'],
  rootKey: '',
  now: () => hub.now,
  ...(prefs !== undefined && { prefs }),
});

describe('job community', () => {
  it('is registered for login and interval', () => {
    expect(backgroundJobs().find((j) => j.name === 'community')).toMatchObject({
      order: 90,
      triggers: ['login', 'interval'],
    });
  });

  it('starts the client on login without waiting and ticks it on interval', async () => {
    const hub = new CommunityHub();
    const a = await hub.user();
    const prefs = new Prefs(null);
    await job.run(context(hub, a, prefs), { org: 'o1', trigger: 'login', generation: 0 });
    expect(a.store.getState().community.phase).toBe('loading');
    await until(() => a.store.getState().community.loaded && hub.calls.length === 5);
    expect(a.store.getState().community).toMatchObject({ phase: 'ready', key: 'ready' });
    const client = communityOf(a.store);
    await job.run(context(hub, a, prefs), { org: 'o2', trigger: 'login', generation: 0 });
    expect(communityOf(a.store)).toBe(client);
    hub.calls.length = 0;
    await job.run(context(hub, a, prefs), { org: 'o1', trigger: 'interval', generation: 0 });
    expect(hub.calls).toEqual(['GET /v1/community', 'GET /v1/community/requests?limit=20']);

    const b = await hub.user();
    await job.run(context(hub, b), { org: 'o1', trigger: 'interval', generation: 0 });
    expect(b.store.getState().community).toMatchObject({ phase: 'ready', key: 'waiting' });
  });
});
