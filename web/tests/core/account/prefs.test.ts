// Browser preferences of the community (14-community.md section 6): the chosen display name and the pinned author
// keys per viewer; malformed or refused storage behaves as empty.
import { describe, expect, it } from 'vitest';
import { Prefs, type PrefStorage } from '../../../src/core/account/prefs';

class MemoryStorage implements PrefStorage {
  readonly items = new Map<string, string>();
  getItem(key: string) {
    return this.items.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.items.set(key, value);
  }
  removeItem(key: string) {
    this.items.delete(key);
  }
}

describe('community preferences', () => {
  it('remembers the display name per user and forgets an empty one', () => {
    const storage = new MemoryStorage();
    const prefs = new Prefs(storage);
    expect(prefs.communityName('u1')).toBeNull();
    prefs.setCommunityName('u1', 'Anna');
    expect(prefs.communityName('u1')).toBe('Anna');
    expect(prefs.communityName('u2')).toBeNull();
    prefs.setCommunityName('u1', '');
    expect(prefs.communityName('u1')).toBeNull();
    expect(storage.items.size).toBe(0);
  });

  it('keeps the pinned author keys per viewer and reads malformed data as empty', () => {
    const storage = new MemoryStorage();
    const prefs = new Prefs(storage);
    expect(prefs.communityKeys('u1')).toEqual({});
    prefs.setCommunityKeys('u1', { a: 'k1' });
    expect(prefs.communityKeys('u1')).toEqual({ a: 'k1' });
    expect(storage.items.get('agent-platform.community-keys.u1')).toBe('{"a":"k1"}');
    for (const raw of ['not json', '[1]', 'null', '"x"']) {
      storage.items.set('agent-platform.community-keys.u2', raw);
      expect(prefs.communityKeys('u2')).toEqual({});
    }
    storage.items.set('agent-platform.community-keys.u2', '{"a":"k","b":1}');
    expect(prefs.communityKeys('u2')).toEqual({ a: 'k' });
    expect(new Prefs(null).communityKeys('u1')).toEqual({});
  });

  it('remembers the agents whose pairing this browser confirmed, per user (ADR-082)', () => {
    const storage = new MemoryStorage();
    const prefs = new Prefs(storage);
    expect(prefs.confirmedAgents('u1')).toEqual({});
    prefs.addConfirmedAgent('u1', 'a1', 'c1');
    prefs.addConfirmedAgent('u1', 'a2', 'c2');
    expect(prefs.confirmedAgents('u1')).toEqual({ a1: 'c1', a2: 'c2' });
    expect(prefs.confirmedAgents('u2')).toEqual({});
    storage.items.set('agent-platform.confirmed-agents.u2', 'not json');
    expect(prefs.confirmedAgents('u2')).toEqual({});
  });

  it('remembers the handled credential-key losses per user and reads malformed data as none', () => {
    const storage = new MemoryStorage();
    const prefs = new Prefs(storage);
    expect(prefs.handledLosses('u1')).toEqual([]);
    prefs.setHandledLosses('u1', ['d1 5']);
    expect(prefs.handledLosses('u1')).toEqual(['d1 5']);
    for (const raw of ['not json', '{}', 'null']) {
      storage.items.set('agent-platform.handled-losses.u2', raw);
      expect(prefs.handledLosses('u2')).toEqual([]);
    }
    storage.items.set('agent-platform.handled-losses.u2', '["a",1]');
    expect(prefs.handledLosses('u2')).toEqual(['a']);
  });
});
