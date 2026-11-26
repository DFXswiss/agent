// KOR-52 (HOT-05): indexes rebuilt after a store update keep the objects of unchanged titles and agent names, so the
// rows that show them do not render again during a burst of pulled events; changed ones are new objects.
import { describe, expect, it } from 'vitest';
import { AGENT_NAME_KIND, selectAgentName } from '../../../src/core/agents/naming';
import { sameAs, scopeIdOf } from '../../../src/core/store/selectors';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { selectTitle, TITLE_KIND } from '../../../src/core/tasks/directory';
import { type TaskWorld, taskWorld } from '../tasks/world';

function entry(t: TaskWorld, seq: number, kind: string, task: string, body: unknown, bad = false): EventEntry {
  const header = { id: `s${seq}`, kind, task, author: t.me.id };
  return {
    org: t.org,
    scopeId: t.wg,
    scopeSeq: seq,
    receivedAt: null,
    event: { header },
    status: bad ? 'unverifiable' : 'verified',
    code: bad ? 'signature_invalid' : null,
    header: bad ? null : header,
    body,
  };
}

describe('sameAs', () => {
  it('keeps the previous object only when its own fields are equal', () => {
    const previous = { status: 'verified', text: 'a' };
    expect(sameAs(undefined, { status: 'verified' })).toEqual({ status: 'verified' });
    expect(sameAs(previous, { status: 'verified', text: 'a' })).toBe(previous);
    const changed = { status: 'verified', text: 'b' };
    expect(sameAs(previous, changed)).toBe(changed);
    const longer = { status: 'verified', text: 'a', code: 'x' };
    expect(sameAs(previous, longer)).toBe(longer);
  });

  it('names the scope of an index, none for an empty one', () => {
    expect(scopeIdOf({ entries: new Map() })).toBeNull();
  });
});

describe('stable titles and names', () => {
  it('keep unchanged objects across updates of their scope', async () => {
    const t = await taskWorld();
    putEvents(t.store, t.wg, [
      entry(t, 1, TITLE_KIND, 'a', { title: 'First' }),
      entry(t, 2, AGENT_NAME_KIND, 'a', { name: 'Doc' }),
      entry(t, 3, TITLE_KIND, 'x', null, true),
    ]);
    const title = () => selectTitle(t.wg, 'a', null)(t.store.getState());
    const broken = () => selectTitle(t.wg, 'x', 's3')(t.store.getState());
    const name = () => selectAgentName(t.wg, 'a', null)(t.store.getState());
    const brokenName = () => selectAgentName(t.wg, 'x', 's3')(t.store.getState());
    const before = [title(), broken(), name(), brokenName()];
    putEvents(t.store, t.wg, [entry(t, 4, TITLE_KIND, 'b', { title: 'Other' })]);
    const after = [title(), broken(), name(), brokenName()];
    after.forEach((value, index) => expect(value).toBe(before[index]));
    // a new title of the task is a new object
    putEvents(t.store, t.wg, [entry(t, 5, TITLE_KIND, 'a', { title: 'Second' })]);
    expect(title()).toEqual({ status: 'verified', text: 'Second' });
    // an empty scope has no index to keep
    t.store.update('events', (events) => ({ ...events, [t.wg]: { entries: new Map() } }));
    expect(title()).toEqual({ status: 'missing' });
    expect(name()).toEqual({ status: 'automatic' });
  });
});
