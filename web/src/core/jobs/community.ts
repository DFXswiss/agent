// Background job `community` (14-community.md section 6): the community client of this sign-in (join, key, serving
// requests, unread count; `core/community/engine.ts`). It is not bound to an org: the runner fires it for every org,
// and the client runs once per sign-in (`login`, not awaited so that unlocking never waits for it) and at most once a
// minute on `interval` (every 10 minutes). A hub without the community stops it quietly.
import { community } from '../community/engine';
import type { JobRegistration } from './index';

export const job: JobRegistration = {
  name: 'community',
  order: 90,
  triggers: ['login', 'interval'],
  run: async (context, run) => {
    const client = community({
      store: context.store,
      client: context.client,
      now: context.now,
      ...(context.prefs !== undefined && { prefs: context.prefs }),
    });
    if (run.trigger === 'login') void client.start();
    else await client.tick();
  },
};
