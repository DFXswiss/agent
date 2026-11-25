// The action record of a session's platform browser: forward pages up to the head, entries without picture data,
// a bounded set of kept pictures (the newest after a pass), pictures read again on demand, live entries, neutral rows
// for unreadable entries, de-duplication, retry after a failed page and stop.
import { describe, expect, it } from 'vitest';
import { ActionRecord, PICTURES, RECORD_PAGE, recordTarget, RETRY_MS } from '../../../src/core/browser/record';
import { HubError } from '../../../src/core/net/signedFetch';
import { ManualTimers } from '../net/stream';
import { action, jpegOf, OTHER, read, RecordHub, SESSION, settle, stored, TARGET, WG } from './world';

const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, index) => from + index);

function setup(seqs: number[] = []) {
  const hub = new RecordHub(seqs.map((seq) => read(seq)));
  const timers = new ManualTimers();
  const record = new ActionRecord(hub.context(timers), TARGET);
  return { hub, timers, record };
}

describe('reading the record', () => {
  it('builds the page target of the events route', () => {
    expect(recordTarget(TARGET, 7)).toBe(
      `/v1/scopes/${WG}/events?after=7&limit=${RECORD_PAGE}&kinds=browser.action&session=${SESSION}`,
    );
    expect(recordTarget(TARGET, 0, 1)).toContain('limit=1');
  });

  it('reads every page up to the head, keeps no picture data in the entries and only the newest pictures', async () => {
    const { hub, record } = setup(range(1, 75));
    const changes: number[] = [];
    const unsubscribe = record.subscribe(() => changes.push(record.state.entries.length));
    await record.pull();
    unsubscribe();
    expect(hub.requests).toHaveLength(4);
    expect(record.state).toMatchObject({ loading: false, complete: true, error: null });
    expect(record.state.entries.map((entry) => entry.scopeSeq)).toEqual(range(1, 75));
    expect(record.state.entries[0]!.action!.image).toEqual({ type: 'image/jpeg', width: 960, height: 600 });
    expect(record.state.entries[0]!.at).toBe(Date.parse('2026-11-25T12:00:00.000Z'));
    expect(changes.at(-1)).toBe(75);
    expect(record.pictures).toBe(PICTURES);
    // a kept picture comes from memory, an older one is read again
    expect(await record.picture(75)).toEqual(jpegOf(75));
    expect(hub.requests).toHaveLength(4);
    expect(await record.picture(1)).toEqual(jpegOf(1));
    expect(hub.requests.at(-1)).toBe(recordTarget(TARGET, 0, 1));
    expect(record.pictures).toBe(PICTURES);
  });

  it('keeps unreadable entries as neutral rows and leaves out other sessions and kinds', async () => {
    const hub = new RecordHub([
      read(1, { status: 'unverifiable' }),
      read(2, { status: 'locked' }),
      read(3, { body: { broken: true } }),
      read(4, { session: OTHER }),
      read(5, { kind: 'session.output' }),
      read(6, { body: action(6, { image: null, image_omitted: 'unavailable' }) }),
    ]);
    const record = new ActionRecord(hub.context(), TARGET);
    await record.pull();
    expect(record.state.entries.map((entry) => [entry.scopeSeq, entry.code, entry.action === null])).toEqual([
      [1, 'signature_invalid', true],
      [2, 'locked', true],
      [3, 'body_invalid', true],
      [6, null, false],
    ]);
    expect(record.pictures).toBe(0);
    // no picture: neither for a row without action, nor without image, nor for an unknown seq
    expect(await record.picture(1)).toBeNull();
    expect(await record.picture(6)).toBeNull();
    expect(await record.picture(99)).toBeNull();
    expect(hub.requests).toHaveLength(1);
  });

  it('continues from the last position, drops events outside the page and de-duplicates', async () => {
    const { hub, record } = setup([1, 2]);
    await record.pull();
    hub.put([read(3)]);
    // an answer with an event at or below the position and one beyond the head is filtered
    hub.request.mockImplementationOnce(async () => ({
      events: [stored(read(2)), stored(read(3)), stored(read(9))],
      next_after: null,
      head: 3,
    }));
    await record.pull();
    expect(hub.requests).toHaveLength(1);
    expect(record.state.entries.map((entry) => entry.scopeSeq)).toEqual([1, 2, 3]);
    await record.pull();
    expect(hub.requests.at(-1)).toContain('after=3');
    expect(record.state.entries).toHaveLength(3);
  });

  it('reads once more when asked while reading', async () => {
    const { hub, record } = setup([1]);
    const first = record.pull();
    void record.pull();
    await first;
    await settle();
    expect(hub.requests).toHaveLength(2);
  });

  it('reads a failed page again after RETRY_MS and reports the code', async () => {
    const { hub, timers, record } = setup([1]);
    hub.fail = new HubError('rate_limited', 429);
    await record.pull();
    expect(record.state).toMatchObject({ loading: false, complete: false, error: 'rate_limited' });
    expect(timers.pending).toEqual([RETRY_MS]);
    hub.fail = null;
    hub.malformed = true;
    timers.advance(RETRY_MS);
    await settle();
    expect(record.state.error).toBe('unavailable');
    hub.request.mockRejectedValueOnce(new Error('network'));
    hub.malformed = false;
    timers.advance(RETRY_MS);
    await settle();
    expect(record.state.error).toBe('unavailable');
    // a pull before the retry cancels it
    await record.pull();
    expect(timers.pending).toEqual([]);
    expect(record.state).toMatchObject({ error: null, complete: true });
    expect(record.state.entries).toHaveLength(1);
  });

  it('ignores answers and failures after stop', async () => {
    const { hub, timers, record } = setup([1]);
    const pending = record.pull();
    record.stop();
    await pending;
    expect(record.state.entries).toEqual([]);
    await record.pull();
    expect(hub.requests).toHaveLength(1);
    const failing = setup([1]);
    failing.hub.fail = new HubError('unavailable', 503);
    const failed = failing.record.pull();
    failing.record.stop();
    await failed;
    expect(failing.timers.pending).toEqual([]);
    expect(failing.record.state.error).toBeNull();
    // stop also cancels a waiting retry
    const retrying = setup([1]);
    retrying.hub.fail = new HubError('unavailable', 503);
    await retrying.record.pull();
    retrying.record.stop();
    expect(retrying.timers.pending).toEqual([]);
    expect(timers.pending).toEqual([]);
  });
});

describe('live entries', () => {
  it('adds a browser_action frame of the scope and ignores malformed ones', async () => {
    const { record } = setup([1]);
    await record.pull();
    for (const data of [
      null,
      { scope_id: 'other', scope_seq: 5, event: {} },
      { scope_id: WG, scope_seq: -1, event: {} },
      { scope_id: WG, scope_seq: 5, event: null },
    ]) {
      await record.receive(data);
    }
    expect(record.state.entries).toHaveLength(1);
    await record.receive({ scope_id: WG, ...stored(read(5)) });
    await record.receive({ scope_id: WG, ...stored(read(3)) });
    await record.receive({ scope_id: WG, ...stored(read(5)) });
    expect(record.state.entries.map((entry) => entry.scopeSeq)).toEqual([1, 3, 5]);
    expect(await record.picture(5)).toEqual(jpegOf(5));
    record.stop();
    await record.receive({ scope_id: WG, ...stored(read(6)) });
    expect(record.state.entries).toHaveLength(3);
  });
});

describe('pictures read again', () => {
  async function older() {
    const t = setup(range(1, PICTURES + 1));
    await t.record.pull();
    t.hub.requests.length = 0;
    return t;
  }

  it('reads a picture once for concurrent requests', async () => {
    const { hub, record } = await older();
    const [a, b] = await Promise.all([record.picture(1), record.picture(1)]);
    expect(a).toEqual(jpegOf(1));
    expect(b).toEqual(jpegOf(1));
    expect(hub.requests).toHaveLength(1);
  });

  it('answers null when the picture cannot be read again', async () => {
    const { hub, record } = await older();
    hub.fail = new HubError('unavailable', 503);
    expect(await record.picture(1)).toBeNull();
    hub.fail = null;
    hub.malformed = true;
    expect(await record.picture(1)).toBeNull();
    hub.malformed = false;
    hub.request.mockImplementationOnce(async () => ({ events: [], next_after: null, head: 40 }));
    expect(await record.picture(1)).toBeNull();
    hub.request.mockImplementationOnce(async () => ({
      events: [stored(read(1, { session: OTHER }))],
      next_after: null,
      head: 40,
    }));
    expect(await record.picture(1)).toBeNull();
    hub.request.mockImplementationOnce(async () => ({
      events: [stored(read(1, { body: action(1, { image: null }) }))],
      next_after: null,
      head: 40,
    }));
    expect(await record.picture(1)).toBeNull();
    const late = record.picture(1);
    record.stop();
    expect(await late).toBeNull();
  });
});
