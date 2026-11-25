// The live picture: frames of another session, of another than the newest open browser session and with a seq not
// above the last shown one are dropped before opening; a frame that does not open keeps its code; frames are opened
// in arrival order; the age label after 30 s.
import { describe, expect, it, vi } from 'vitest';
import { AGE_LABEL_MS, frameAge, LivePicture, type OpenedFrame, showsAge } from '../../../src/core/browser/live';
import { CryptoError } from '../../../src/core/crypto/errors';
import { BROWSER, BROWSER2, OTHER, SESSION } from './world';

const frame = (seq: number, browser = BROWSER) => ({ browser, seq, ts: '2026-11-25T12:00:00.000Z' });
const data = (seq: number, browser = BROWSER, session = SESSION) => ({
  session_id: session,
  frame: frame(seq, browser),
});
const opened = (input: unknown): OpenedFrame => {
  const { browser, seq } = input as { browser: string; seq: number };
  return { browser, seq, ts: 1000, jpeg: new Uint8Array([seq]) };
};

function setup() {
  let current: string | null = BROWSER;
  const open = vi.fn(async (input: unknown) => opened(input));
  const live = new LivePicture({ session: SESSION, open, current: () => current });
  const changes: number[] = [];
  live.subscribe(() => changes.push(live.state.frame?.seq ?? -1));
  return { live, open, changes, setCurrent: (id: string | null) => (current = id) };
}

describe('live picture', () => {
  it('shows newer frames of the newest open browser session and drops the rest before opening', async () => {
    const t = setup();
    await t.live.receive(data(3));
    expect(t.live.state).toMatchObject({ frame: { browser: BROWSER, seq: 3 }, error: null });
    for (const dropped of [
      data(3),
      data(2),
      data(4, BROWSER2),
      data(4, BROWSER, OTHER),
      { session_id: SESSION, frame: null },
      { session_id: SESSION, frame: { browser: BROWSER, seq: 'x' } },
      { session_id: SESSION, frame: { browser: 7, seq: 9 } },
      null,
    ]) {
      await t.live.receive(dropped);
    }
    expect(t.open).toHaveBeenCalledTimes(1);
    // a restarted browser session (another ID) starts its own sequence
    t.setCurrent(BROWSER2);
    await t.live.receive(data(0, BROWSER2));
    expect(t.live.state.frame).toMatchObject({ browser: BROWSER2, seq: 0 });
    t.setCurrent(null);
    await t.live.receive(data(1, BROWSER2));
    expect(t.changes).toEqual([3, 0]);
  });

  it('keeps the code of a frame that does not open and drops a frame that opened as another', async () => {
    const t = setup();
    t.open.mockRejectedValueOnce(new CryptoError('decrypt_failed'));
    await t.live.receive(data(1));
    expect(t.live.state.error).toBe('decrypt_failed');
    t.open.mockRejectedValueOnce(new Error('odd'));
    await t.live.receive(data(1));
    expect(t.live.state.error).toBe('decrypt_failed');
    t.open.mockRejectedValueOnce('odd');
    await t.live.receive(data(1));
    expect(t.live.state.error).toBe('decrypt_failed');
    t.open.mockRejectedValueOnce({ code: 'keys_pending' });
    await t.live.receive(data(1));
    expect(t.live.state.error).toBe('keys_pending');
    t.open.mockResolvedValueOnce({ ...opened(frame(5)), browser: BROWSER2 });
    await t.live.receive(data(5));
    expect(t.live.state.frame).toBeNull();
    await t.live.receive(data(5));
    expect(t.live.state).toMatchObject({ frame: { seq: 5 }, error: null });
  });

  it('opens in arrival order, so a slow older frame does not replace a newer one', async () => {
    const t = setup();
    let release!: () => void;
    t.open.mockImplementationOnce(
      (input) =>
        new Promise((resolve) => {
          release = () => resolve(opened(input));
        }),
    );
    const first = t.live.receive(data(1));
    const second = t.live.receive(data(2));
    await new Promise((resolve) => setTimeout(resolve, 0));
    release();
    await Promise.all([first, second]);
    expect(t.changes).toEqual([1, 2]);
  });

  it('ignores frames after stop, also one being opened', async () => {
    const t = setup();
    let release!: () => void;
    t.open.mockImplementationOnce(
      (input) =>
        new Promise((resolve) => {
          release = () => resolve(opened(input));
        }),
    );
    const pending = t.live.receive(data(1));
    await new Promise((resolve) => setTimeout(resolve, 0));
    t.live.stop();
    release();
    await pending;
    await t.live.receive(data(2));
    expect(t.live.state.frame).toBeNull();
    const failing = setup();
    let reject!: () => void;
    failing.open.mockImplementationOnce(
      () =>
        new Promise((_resolve, fail) => {
          reject = () => fail(new CryptoError('decrypt_failed'));
        }),
    );
    const failed = failing.live.receive(data(1));
    await new Promise((resolve) => setTimeout(resolve, 0));
    failing.live.stop();
    reject();
    await failed;
    expect(failing.live.state.error).toBeNull();
  });

  it('labels a picture older than 30 s with its age', () => {
    expect(frameAge(1000, 500)).toBe(0);
    expect(frameAge(1000, 1000 + AGE_LABEL_MS)).toBe(AGE_LABEL_MS);
    expect(showsAge(AGE_LABEL_MS)).toBe(false);
    expect(showsAge(AGE_LABEL_MS + 1)).toBe(true);
  });

  it('unsubscribes a listener', async () => {
    const t = setup();
    const listener = vi.fn();
    const unsubscribe = t.live.subscribe(listener);
    unsubscribe();
    await t.live.receive(data(1));
    expect(listener).not.toHaveBeenCalled();
  });
});
