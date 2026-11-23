// The `community` slice (12-community.md section 6): unread count from the hub's count and the loaded messages,
// message order, removal (by frame `delete`, by ID) that never brings content back, and the frame handlers.
import { describe, expect, it } from 'vitest';
import { createStore } from '../../../src/core/store';
import {
  type CommunityMessage,
  countUnread,
  frameData,
  frames,
  initialCommunity,
  onCommunityFrame,
  patchCommunity,
  putMessages,
  removeMessage,
} from '../../../src/core/store/slices/community';

const ME = 'me';
const message = (seq: number, author = 'other', extra: Partial<CommunityMessage> = {}): CommunityMessage => ({
  seq,
  id: `id-${seq}`,
  author,
  at: seq,
  removed: false,
  status: 'verified',
  name: 'N',
  text: `t${seq}`,
  moderator: false,
  ...extra,
});

describe('unread count', () => {
  it('starts from the hub count and follows reading, new and removed messages', () => {
    const store = createStore();
    patchCommunity(store, { me: ME, unreadBase: 3, baseRead: 1, baseHead: 4, lastRead: 1 });
    expect(store.getState().community.unread).toBe(3);
    putMessages(store, [message(2), message(3, ME), message(4), message(5), message(6, ME)]);
    expect(store.getState().community.unread).toBe(4);
    patchCommunity(store, { lastRead: 2 });
    expect(store.getState().community.unread).toBe(3);
    removeMessage(store, { seq: 4 });
    expect(store.getState().community.unread).toBe(2);
    removeMessage(store, { id: 'id-2' });
    expect(store.getState().community.unread).toBe(2);
    removeMessage(store, { seq: 5 });
    expect(store.getState().community.unread).toBe(1);
    patchCommunity(store, { lastRead: 6 });
    expect(store.getState().community.unread).toBe(0);
    expect(countUnread({ ...initialCommunity(), unreadBase: 1, baseHead: 5, lastRead: 3, baseRead: 0 })).toBe(1);
  });

  it('keeps the order by seq, ignores unknown removals and never restores a removed message', () => {
    const store = createStore();
    putMessages(store, [message(3), message(1)], { loaded: true });
    expect(store.getState().community.messages.map((m) => m.seq)).toEqual([1, 3]);
    const before = store.getState().community;
    removeMessage(store, { seq: 9 });
    removeMessage(store, { id: 'x' });
    expect(store.getState().community).toBe(before);
    removeMessage(store, { seq: 3 });
    removeMessage(store, { seq: 3 });
    putMessages(store, [message(3), message(2)]);
    expect(store.getState().community.messages).toMatchObject([
      { seq: 1, text: 't1' },
      { seq: 2, text: 't2' },
      { seq: 3, removed: true, text: null, name: null, status: 'verified' },
    ]);
    patchCommunity(store, { loaded: true });
  });
});

describe('frames', () => {
  it('parses frame data', () => {
    expect(frameData({ change: 'message', seq: 3 })).toEqual({ change: 'message', seq: 3 });
    expect(frameData(null)).toBeNull();
    expect(frameData({ change: 1, seq: 3 })).toBeNull();
    expect(frameData({ change: 'x', seq: 1.5 })).toBeNull();
  });

  it('drops a deleted message at once and hands frames to the registered engine', () => {
    const store = createStore();
    frames.community(store, { change: 'message', seq: 1 });
    frames.hello(store);
    frames.resync(store);
    const seen: string[] = [];
    onCommunityFrame(store, (frame) => seen.push(`${frame.change}:${frame.seq}`));
    putMessages(store, [message(1)]);
    frames.community(store, { change: 'delete', seq: 1 });
    frames.community(store, { change: 'message' });
    frames.hello(store);
    frames.resync(store);
    expect(store.getState().community.messages[0]!.removed).toBe(true);
    expect(seen).toEqual(['delete:1', 'hello:0', 'hello:0']);
  });
});
