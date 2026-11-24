// The own display name for a new membership (KOR-36): read from the user's orgs in the given order, profiles pulled
// once when not in the store; a failed pull or an org without the user's profile moves on; '' if none has one.
import { describe, expect, it, vi } from 'vitest';
import { ownDisplayName } from '../../../src/core/account/own-name';
import { createStore } from '../../../src/core/store';
import { putEvents } from '../../../src/core/store/slices/events';
import type { Store } from '../../../src/core/store/store';
import type { TasksContext } from '../../../src/core/tasks/pull';

const USER = 'user-1';

function profile(store: Store, org: string, author: string, name: string): void {
  putEvents(store, org, [
    {
      org,
      scopeId: org,
      scopeSeq: 1,
      receivedAt: null,
      event: {},
      status: 'verified',
      code: null,
      header: { kind: 'member.profile', ref: author, author },
      body: { display_name: name },
    },
  ]);
}

function context(pages: Record<string, () => unknown>, onIngest: (org: string) => void = () => undefined) {
  const store = createStore();
  const request = vi.fn(async (_method: string, target: string) => {
    const org = decodeURIComponent(target.split('/')[3]!);
    return pages[org]!();
  });
  const tasks: TasksContext = {
    store,
    client: { request } as unknown as TasksContext['client'],
    refreshLog: async () => true,
    ingest: async (org) => onIngest(org),
    now: () => 0,
  };
  return { store, request, tasks };
}

describe('ownDisplayName', () => {
  it('takes the first org with the own profile, pulling what is missing', async () => {
    const empty = () => ({ events: [], next_after: null });
    const { store, request, tasks } = context(
      {
        a: empty,
        b: () => {
          throw new Error('offline');
        },
        c: empty,
      },
      (org) => {
        if (org === 'c') profile(store, 'c', USER, 'Cleo');
      },
    );
    // another member's profile in a does not count
    profile(store, 'a', 'someone-else', 'Other');
    expect(await ownDisplayName(tasks, ['a', 'b', 'c'], USER)).toBe('Cleo');
    expect(request).toHaveBeenCalledTimes(3);
    // in the store already: no pull
    profile(store, 'd', USER, 'Dee');
    expect(await ownDisplayName(tasks, ['d'], USER)).toBe('Dee');
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("is '' without an org that has the own profile", async () => {
    const { tasks } = context({ a: () => ({ events: [], next_after: null }) });
    expect(await ownDisplayName(tasks, [], USER)).toBe('');
    expect(await ownDisplayName(tasks, ['a'], USER)).toBe('');
  });
});
