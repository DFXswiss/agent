// U-WEB-org-pages: the pages of the hub's bounded organization lists (ADR-086 section 2): `before`/`limit`, the
// paging fields, the old hub (refuses the parameters: once without them, remembered per client) and the re-read of a
// loaded range.
import { describe, expect, it } from 'vitest';
import { actionErrorCode } from '../../../src/core/org/batch';
import {
  appendRows,
  lacksListPaging,
  LIMIT,
  LIMIT_MAX,
  type ListPage,
  readHubPage,
  readPages,
} from '../../../src/core/org/pages';
import { HubError } from '../../../src/core/net/signedFetch';
import { FakeClient } from '../tasks/world';

const PATH = '/v1/x/list';

async function refusal(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (error) {
    return actionErrorCode(error);
  }
  throw new Error('not refused');
}

describe('one page', () => {
  it('sends before and limit and reads next_before and truncated', async () => {
    const client = new FakeClient().on(`GET ${PATH}`, (_body, target) =>
      String(target).includes('before=')
        ? { items: [3], next_before: null }
        : { items: [1, 2], next_before: 'c/1', truncated: true },
    );
    expect(LIMIT).toBe(100);
    expect(LIMIT_MAX).toBe(200);
    expect(await readHubPage(client, PATH, 'items', null, 2)).toEqual({
      items: [1, 2],
      nextBefore: 'c/1',
      truncated: true,
      body: { items: [1, 2], next_before: 'c/1', truncated: true },
    });
    expect(await readHubPage(client, PATH, 'items', 'c/1', 2)).toEqual({
      items: [3],
      nextBefore: null,
      truncated: false,
      body: { items: [3], next_before: null },
    });
    expect(client.calls.map((call) => call.target)).toEqual([`${PATH}?limit=2`, `${PATH}?before=c%2F1&limit=2`]);
    expect(lacksListPaging(client)).toBe(false);
  });

  it('takes an answer without next_before as the end and refuses malformed ones', async () => {
    const client = new FakeClient().on(`GET ${PATH}`, () => ({ items: [] }));
    expect(await readHubPage(client, PATH, 'items', null, 5)).toEqual({
      items: [],
      nextBefore: null,
      truncated: false,
      body: { items: [] },
    });
    for (const body of [null, { items: 'x' }, { other: [] }, { items: [], next_before: 5 }]) {
      client.on(`GET ${PATH}`, () => body);
      expect(await refusal(readHubPage(client, PATH, 'items', null, 5))).toBe('unavailable');
    }
  });

  it('repeats once without the parameters on an old hub and remembers it', async () => {
    const client = new FakeClient().on(`GET ${PATH}`, (_body, target) => {
      if (String(target).includes('?')) throw new HubError('request_invalid', 400);
      return { items: [1, 2], truncated: true };
    });
    expect(await readHubPage(client, PATH, 'items', null, 5)).toEqual({
      items: [1, 2],
      nextBefore: null,
      truncated: true,
      body: { items: [1, 2], truncated: true },
    });
    expect(lacksListPaging(client)).toBe(true);
    expect(await readHubPage(client, PATH, 'items', null, 5)).toMatchObject({ items: [1, 2] });
    // an older page of the whole list is empty
    expect(await readHubPage(client, PATH, 'items', 'c', 5)).toEqual({
      items: [],
      nextBefore: null,
      truncated: false,
      body: {},
    });
    expect(client.calls.map((call) => call.target)).toEqual([`${PATH}?limit=5`, PATH, PATH]);
  });

  it('passes other errors on', async () => {
    const client = new FakeClient().on(`GET ${PATH}`, () => {
      throw new HubError('forbidden', 403);
    });
    expect(await refusal(readHubPage(client, PATH, 'items', null, 5))).toBe('forbidden');
    expect(lacksListPaging(client)).toBe(false);
    client.on(`GET ${PATH}`, () => {
      throw new TypeError('offline');
    });
    await expect(readHubPage(client, PATH, 'items', null, 5)).rejects.toBeInstanceOf(TypeError);
    expect(lacksListPaging(client)).toBe(false);
  });
});

describe('a loaded range', () => {
  const pages: Record<string, ListPage<string>> = {
    first: { rows: ['a', 'b'], nextBefore: 'p2', truncated: false },
    p2: { rows: ['b', 'c'], nextBefore: 'p3', truncated: true },
    p3: { rows: ['d'], nextBefore: null, truncated: false },
  };
  const read = async (before: string | null) => pages[before ?? 'first']!;
  const id = (row: string) => row;

  it('reads the first pages, each row once, and stops at the end', async () => {
    expect(await readPages(read, 1, id)).toEqual({ rows: ['a', 'b'], nextBefore: 'p2', truncated: false, pages: 1 });
    expect(await readPages(read, 2, id)).toEqual({
      rows: ['a', 'b', 'c'],
      nextBefore: 'p3',
      truncated: true,
      pages: 2,
    });
    expect(await readPages(read, 9, id)).toEqual({
      rows: ['a', 'b', 'c', 'd'],
      nextBefore: null,
      truncated: true,
      pages: 3,
    });
  });

  it('appends an older page without the rows already loaded', () => {
    expect(appendRows(['a', 'b'], ['b', 'c'], id)).toEqual(['a', 'b', 'c']);
  });
});
