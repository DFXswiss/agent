// Slice `joined` (KOR-36): the confirmation after joining an org from an invitation.
import { describe, expect, it } from 'vitest';
import { createStore } from '../../../src/core/store';
import { dismissJoined, noteJoined } from '../../../src/core/store/slices/joined';

describe('joined slice', () => {
  it('holds one notice until dismissed', () => {
    const store = createStore();
    expect(store.getState().joined.notice).toBeNull();
    const before = store.getState().joined;
    dismissJoined(store);
    expect(store.getState().joined).toBe(before);
    noteJoined(store, { org: 'o1', orgName: 'Grüße AG' });
    expect(store.getState().joined.notice).toEqual({ org: 'o1', orgName: 'Grüße AG' });
    dismissJoined(store);
    expect(store.getState().joined.notice).toBeNull();
  });
});
