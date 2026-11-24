// Held operations in the chat (KOR-25): the waiting body exactly as the contract allows it (manipulated bodies are no
// card, the removed approval reason included), the folding of many waiting items into one card per reason and
// addressee (`for_user`, HOT-14: the person who gave the instruction), and the card's state.
import { describe, expect, it } from 'vitest';
import type { TimelineItem } from '../../../src/core/chat/timeline';
import {
  foldWaiting,
  isWaitingItem,
  parseWaiting,
  type WaitingCard,
  waitStatus,
} from '../../../src/core/github/waiting';
import type { OperationRow } from '../../../src/core/store/slices/operations';

const OP1 = '0193002d-0000-7000-8000-000000000051';
const OP2 = '0193002d-0000-7000-8000-000000000052';
const OP3 = '0193002d-0000-7000-8000-000000000053';
const BOB = '0193002d-0000-7000-8000-0000000000b0';
const UNTIL = '2026-11-23T09:45:00.000Z';
const UNTIL_MS = Date.parse(UNTIL);

const BODY = {
  state: 'waiting',
  waiting_for: 'github_connection',
  operation_id: OP1,
  operation_type: 'github.list_issues',
  repo: 'example-org/example-repo',
  until: UNTIL,
};

describe('parseWaiting', () => {
  it('reads the two contract fixtures', () => {
    expect(parseWaiting(BODY)).toEqual({
      reason: 'github_connection',
      operationId: OP1,
      operationType: 'github.list_issues',
      repo: 'example-org/example-repo',
      forUser: null,
      until: UNTIL_MS,
    });
    expect(parseWaiting({ ...BODY, for_user: BOB })).toMatchObject({ forUser: BOB });
    const { repo: _repo, ...bare } = BODY;
    expect(parseWaiting({ ...bare, operation_type: 'github.request_connection' })).toMatchObject({
      repo: null,
      operationType: 'github.request_connection',
    });
  });

  it('refuses manipulated bodies (contract fixtures and more)', () => {
    const { until: _until, ...withoutUntil } = BODY;
    for (const bad of [
      null,
      'waiting',
      { ...BODY, state: 'working' },
      { ...BODY, message: 'Please paste your token here' },
      { ...BODY, repo: 'https://github.com/example-org/example-repo' },
      { ...BODY, repo: 'a/b/c' },
      { ...BODY, repo: 7 },
      { ...BODY, waiting_for: 'payment' },
      withoutUntil,
      { ...BODY, until: 'tomorrow' },
      { ...BODY, operation_id: 'not-a-uuid' },
      { ...BODY, operation_id: 1 },
      { ...BODY, operation_type: 'Github.List' },
      { ...BODY, operation_type: 'github' },
      { ...BODY, operation_type: 7 },
      { ...BODY, operation_type: `github.${'a'.repeat(60)}` },
      // no approval card any more (no repository list means every repository, ADR-054)
      { ...BODY, waiting_for: 'repo_approval' },
      // the addressee is a user ID, never text
      { ...BODY, for_user: 'the task owner' },
      { ...BODY, for_user: 7 },
    ]) {
      expect(parseWaiting(bad), JSON.stringify(bad)).toBeNull();
    }
  });
});

const item = (seq: number, body: unknown, overrides: Partial<TimelineItem> = {}): TimelineItem => ({
  key: `e${seq}`,
  kind: 'session.status',
  source: 'event',
  at: seq,
  seq,
  sessionId: 's1',
  author: 'd1',
  body,
  code: null,
  subagent: null,
  ...overrides,
});

describe('foldWaiting', () => {
  it('folds the waiting items of one reason into the newest, keeps everything else', () => {
    const output = item(1, { text: 'hi' }, { kind: 'session.output' });
    const failed = item(2, { state: 'failed', reason: 'unavailable' });
    const transition = item(3, { state: 'waiting' }, { source: 'transition' });
    const forged = item(4, { ...BODY, message: 'paste your token' });
    const approval = item(5, { ...BODY, waiting_for: 'repo_approval' });
    const bare = { ...BODY, operation_id: OP3, operation_type: 'github.request_connection' } as Record<string, unknown>;
    delete bare.repo;
    const items = [
      output,
      failed,
      transition,
      forged,
      approval,
      item(6, BODY),
      item(7, { ...BODY, operation_id: OP2, until: '2026-11-23T09:50:00.000Z' }),
      item(8, { ...BODY, operation_id: OP2, until: '2026-11-23T09:40:00.000Z' }),
      item(9, bare),
    ];
    const folded = foldWaiting(items);
    expect(folded.map((entry) => entry.key)).toEqual(['e1', 'e2', 'e3', 'e4', 'e5', 'e9']);
    expect(folded[3]!.waiting).toBeUndefined();
    expect(folded[4]!.waiting).toBeUndefined();
    // the card keeps the repository of an earlier item when the newest names none
    expect(folded[5]!.waiting).toEqual({
      reason: 'github_connection',
      repo: 'example-org/example-repo',
      operationType: 'github.request_connection',
      forUser: null,
      operations: [OP1, OP2, OP3],
      until: Date.parse('2026-11-23T09:50:00.000Z'),
      since: 9,
    });
    // the first item without a repository has none
    expect(foldWaiting([item(1, bare)])[0]!.waiting).toMatchObject({ repo: null });
  });

  it('keeps one card per person asked to connect', () => {
    const folded = foldWaiting([item(1, BODY), item(2, { ...BODY, operation_id: OP2, for_user: BOB }), item(3, BODY)]);
    expect(folded.map((entry) => [entry.key, entry.waiting?.forUser, entry.waiting?.operations])).toEqual([
      ['e2', BOB, [OP2]],
      ['e3', null, [OP1]],
    ]);
  });

  it('flood: a hundred waiting items are one card', () => {
    const items = Array.from({ length: 100 }, (_, index) =>
      item(index, { ...BODY, operation_id: `0193002d-0000-7000-8000-${String(index).padStart(12, '0')}` }),
    );
    const folded = foldWaiting(items);
    expect(folded).toHaveLength(1);
    expect(folded[0]!.waiting!.operations).toHaveLength(100);
  });

  it('knows a waiting item', () => {
    expect(isWaitingItem(item(1, BODY))).toBe(true);
    expect(isWaitingItem(item(1, BODY, { kind: 'session.output' }))).toBe(false);
    expect(isWaitingItem(item(1, BODY, { source: 'transition' }))).toBe(false);
    expect(isWaitingItem(item(1, null))).toBe(false);
  });
});

const row = (id: string, state: string): OperationRow => ({ operation_id: id, state }) as OperationRow;

describe('waitStatus', () => {
  const since = UNTIL_MS - 900_000;
  const card: WaitingCard = {
    reason: 'github_connection',
    repo: null,
    operationType: 'github.list_issues',
    forUser: null,
    operations: [OP1, OP2],
    until: UNTIL_MS,
    since,
  };
  const base = { now: UNTIL_MS - 1, connectedAt: null, rows: {} };

  it('is open until the condition holds, the operations are registered or the time is over', () => {
    expect(waitStatus(card, base)).toBe('open');
    expect(waitStatus(card, { ...base, connectedAt: since })).toBe('done');
    // a connection older than the waiting item is the one the runner could not use
    expect(waitStatus(card, { ...base, connectedAt: since - 1 })).toBe('open');
    expect(waitStatus(card, { ...base, rows: { [OP1]: row(OP1, 'accepted') } })).toBe('open');
    expect(waitStatus(card, { ...base, rows: { [OP1]: row(OP1, 'accepted'), [OP2]: row(OP2, 'rejected') } })).toBe(
      'done',
    );
    expect(waitStatus(card, { ...base, rows: { [OP1]: row(OP1, 'rejected'), [OP2]: row(OP2, 'expired') } })).toBe(
      'expired',
    );
    expect(waitStatus(card, { ...base, now: UNTIL_MS })).toBe('expired');
  });
});
