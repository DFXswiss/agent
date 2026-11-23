// KOR-11 (AUD-02 NA-05): the connection slice counts the live channel's `hello` and `resync` frames (`round`); the
// screens load their rows again for every new value.
import { describe, expect, it } from 'vitest';
import { applyFrame, frameHandlers } from '../../../src/core/net/frames';
import { createStore } from '../../../src/core/store/store';

describe('connection round', () => {
  it('counts hello and resync frames and nothing else', () => {
    const store = createStore();
    expect(store.getState().connection.round).toBe(0);
    applyFrame(store, 'hello', { heads: {} }, 0, frameHandlers());
    applyFrame(store, 'resync', { reason: 'buffer_overflow' }, 0, frameHandlers());
    applyFrame(store, 'task', {}, 0, frameHandlers());
    expect(store.getState().connection.round).toBe(2);
  });
});
