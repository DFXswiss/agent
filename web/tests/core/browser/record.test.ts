// The action record of a session's platform browser: the newest page on opening (one request for any length of
// record), older pages on demand, the newest page again after a reconnect (and further back only over what was
// missed), entries without picture data, a bounded set of kept pictures, pictures read again on demand, live entries,
// neutral rows for unreadable entries, de-duplication, retry after a failed read and stop.
import { describe, expect, it } from 'vitest';
import {
  ActionRecord,
  NEWEST,
  PICTURES,
  RECORD_PAGE,
  rangeKey,
  recordTarget,
  RETRY_MS,
} from '../../../src/core/browser/record';
import { HubError } from '../../../src/core/net/signedFetch';
import { ManualTimers } from '../net/stream';
import { action, jpegOf, OTHER, read, RecordHub, SESSION, settle, stored, TARGET, WG } from './world';

const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, index) => from + index);
const seqs = (record: ActionRecord) => record.state.entries.map((entry) => entry.scopeSeq);

function setup(numbers: number[] = []) {
  const hub = new RecordHub(numbers.map((seq) => read(seq)));
  const timers = new ManualTimers();
  const record = new ActionRecord(hub.context(timers), TARGET);
  return { hub, timers, record };
}

describe('reading the record', () => {
  it('names the position without a total while older entries are still to be read', () => {
    expect(rangeKey(true)).toBe('browser.record.range.more');
    expect(rangeKey(false)).toBe('browser.record.range');
  });

  it('builds the page target of the events route, newest first', () => {
    expect(recordTarget(TARGET, NEWEST)).toBe(
      `/v1/scopes/${WG}/events?before=${NEWEST}&limit=${RECORD_PAGE}&kinds=browser.action&session=${SESSION}`,
    );
    expect(recordTarget(TARGET, 7, 1)).toContain('before=7&limit=1');
  });

  it('opens with the newest page only, whatever the length of the record, and reads older pages on demand', async () => {
    const { hub, record } = setup(range(1, 5000));
    const changes: number[] = [];
    const unsubscribe = record.subscribe(() => changes.push(record.state.entries.length));
    await record.pull();
    unsubscribe();
    expect(hub.requests).toEqual([recordTarget(TARGET, NEWEST)]);
    expect(seqs(record)).toEqual(range(5000 - RECORD_PAGE + 1, 5000));
    expect(record.state).toMatchObject({ loading: false, more: true, error: null });
    expect(record.state.entries[0]!.action!.image).toEqual({ type: 'image/jpeg', width: 960, height: 600 });
    expect(record.state.entries[0]!.at).toBe(Date.parse('2026-11-25T12:00:00.000Z'));
    expect(changes.at(-1)).toBe(RECORD_PAGE);
    await record.older();
    expect(hub.requests.at(-1)).toBe(recordTarget(TARGET, 5000 - RECORD_PAGE + 1));
    expect(seqs(record)).toEqual(range(5000 - 2 * RECORD_PAGE + 1, 5000));
    expect(record.pictures).toBe(PICTURES);
    // the most recently read pictures come from memory, an evicted one is read again
    const oldest = 5000 - 2 * RECORD_PAGE + 1;
    expect(await record.picture(oldest)).toEqual(jpegOf(oldest));
    expect(hub.requests).toHaveLength(2);
    expect(await record.picture(5000)).toEqual(jpegOf(5000));
    expect(hub.requests.at(-1)).toBe(recordTarget(TARGET, 5001, 1));
    expect(record.pictures).toBe(PICTURES);
  });

  it('knows when nothing older exists and then reads no further page', async () => {
    const { hub, record } = setup(range(1, RECORD_PAGE + 3));
    await record.pull();
    await record.older();
    expect(seqs(record)).toEqual(range(1, RECORD_PAGE + 3));
    expect(record.state.more).toBe(false);
    await record.older();
    expect(hub.requests).toHaveLength(2);
    const empty = setup();
    await empty.record.older();
    expect(empty.hub.requests).toHaveLength(0);
    await empty.record.pull();
    expect(empty.record.state).toMatchObject({ entries: [], more: false });
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

  it('reads the newest page again after a reconnect and further back only over what was missed', async () => {
    const { hub, record } = setup(range(1, 30));
    await record.pull();
    expect(seqs(record)).toEqual(range(11, 30));
    // nothing new: one request
    await record.pull();
    expect(hub.requests).toHaveLength(2);
    // 45 actions missed: the newest page and back until the held ones are met; the older pages stay on demand
    hub.put(range(31, 75).map((seq) => read(seq)));
    await record.pull();
    expect(hub.requests.slice(2)).toEqual([
      recordTarget(TARGET, NEWEST),
      recordTarget(TARGET, 56),
      recordTarget(TARGET, 36),
    ]);
    expect(seqs(record)).toEqual(range(11, 75));
    expect(record.state.more).toBe(true);
    await record.older();
    expect(hub.requests.at(-1)).toBe(recordTarget(TARGET, 11));
    expect(seqs(record)).toEqual(range(1, 75));
  });

  it('drops events at or above `before` and de-duplicates', async () => {
    const { hub, record } = setup([1, 2]);
    hub.request.mockImplementationOnce(async () => ({
      events: [stored(read(2)), stored(read(2)), stored(read(1))],
      next_before: 5,
    }));
    await record.pull();
    expect(seqs(record)).toEqual([1, 2]);
    hub.request.mockImplementationOnce(async () => ({ events: [stored(read(9))], next_before: 5 }));
    await record.older();
    expect(seqs(record)).toEqual([1, 2]);
  });

  it('reads once more when asked while reading, and not older while reading', async () => {
    const { hub, record } = setup(range(1, 50));
    const first = record.pull();
    void record.pull();
    void record.older();
    await first;
    await settle();
    expect(hub.requests).toEqual([recordTarget(TARGET, NEWEST), recordTarget(TARGET, NEWEST)]);
    // a pull asked during an older read follows it
    const older = record.older();
    void record.pull();
    await older;
    await settle();
    expect(hub.requests.slice(2)).toEqual([recordTarget(TARGET, 31), recordTarget(TARGET, NEWEST)]);
  });

  it('reads a failed page again after RETRY_MS and reports the code', async () => {
    const { hub, timers, record } = setup([1]);
    hub.fail = new HubError('rate_limited', 429);
    await record.pull();
    expect(record.state).toMatchObject({ loading: false, error: 'rate_limited' });
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
    // a read before the retry cancels it
    await record.pull();
    expect(timers.pending).toEqual([]);
    expect(record.state).toMatchObject({ error: null, more: false });
    expect(seqs(record)).toEqual([1]);
  });

  it('reads a failed older page again after RETRY_MS', async () => {
    const { hub, timers, record } = setup(range(1, 25));
    await record.pull();
    hub.fail = new HubError('unavailable', 503);
    await record.older();
    expect(record.state.error).toBe('unavailable');
    hub.fail = null;
    timers.advance(RETRY_MS);
    await settle();
    expect(seqs(record)).toEqual(range(1, 25));
    expect(record.state.error).toBeNull();
  });

  it('ignores answers and failures after stop', async () => {
    const { hub, timers, record } = setup([1]);
    const pending = record.pull();
    record.stop();
    await pending;
    expect(record.state.entries).toEqual([]);
    await record.pull();
    await record.older();
    expect(hub.requests).toHaveLength(1);
    const paging = setup(range(1, 25));
    await paging.record.pull();
    const older = paging.record.older();
    paging.record.stop();
    await older;
    expect(paging.record.state.entries).toHaveLength(RECORD_PAGE);
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
    expect(seqs(record)).toEqual([1, 3, 5]);
    expect(await record.picture(5)).toEqual(jpegOf(5));
    record.stop();
    await record.receive({ scope_id: WG, ...stored(read(6)) });
    expect(record.state.entries).toHaveLength(3);
  });
});

describe('pictures read again', () => {
  async function older() {
    const t = setup(range(1, 40));
    await t.record.pull();
    await t.record.older();
    t.hub.requests.length = 0;
    return t;
  }

  it('reads a picture once for concurrent requests', async () => {
    const { hub, record } = await older();
    const [a, b] = await Promise.all([record.picture(40), record.picture(40)]);
    expect(a).toEqual(jpegOf(40));
    expect(b).toEqual(jpegOf(40));
    expect(hub.requests).toHaveLength(1);
  });

  it('answers null when the picture cannot be read again', async () => {
    const { hub, record } = await older();
    hub.fail = new HubError('unavailable', 503);
    expect(await record.picture(40)).toBeNull();
    hub.fail = null;
    hub.malformed = true;
    expect(await record.picture(40)).toBeNull();
    hub.malformed = false;
    hub.request.mockImplementationOnce(async () => ({ events: [], next_before: null }));
    expect(await record.picture(40)).toBeNull();
    hub.request.mockImplementationOnce(async () => ({
      events: [stored(read(40, { session: OTHER }))],
      next_before: null,
    }));
    expect(await record.picture(40)).toBeNull();
    hub.request.mockImplementationOnce(async () => ({
      events: [stored(read(40, { body: action(40, { image: null }) }))],
      next_before: null,
    }));
    expect(await record.picture(40)).toBeNull();
    const late = record.picture(40);
    record.stop();
    expect(await late).toBeNull();
  });
});
