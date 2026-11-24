// U-WEB-attention (what the situation view shows and offers, KOR-39): the reasons of the contracts with a generic text
// for unknown ones, the next step of every reason, the approval prompt, the verified question of an asking operation,
// the window of a group, the task rows of the loaded entries and the "no task yet" state.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  actionOf,
  approvalText,
  badgeText,
  earlierFrom,
  groupWindow,
  laterFrom,
  reasonKey,
  reasonText,
  REASONS,
  selectTaskRows,
  selectNoTask,
  selectQuestion,
  STEP,
  WINDOW,
  windowAt,
} from '../../../src/core/attention/view';
import { createStore } from '../../../src/core/store';
import { CATEGORIES, groupPage } from '../../../src/core/store/slices/attention';
import { type EventEntry, putEvents } from '../../../src/core/store/slices/events';
import { putOperations } from '../../../src/core/store/slices/operations';
import { putTasks, type TaskRow } from '../../../src/core/store/slices/tasks';
import { WEB_DIR } from '../../helpers';
import { taskRow } from '../tasks/world';
import { record } from './rows';

const OP = '0190f000-0000-7000-8000-0000000000b1';
const DEVICE = '0190f000-0000-7000-8000-0000000000d1';

describe('reasons and actions', () => {
  it('match the closed lists of contracts/enums/attention.json', () => {
    const enums = JSON.parse(readFileSync(join(WEB_DIR, '../contracts/enums/attention.json'), 'utf8')) as {
      categories: { category: string; reasons: string[] }[];
    };
    expect(enums.categories.map((entry) => entry.category)).toEqual([...CATEGORIES]);
    for (const entry of enums.categories)
      expect(REASONS[entry.category as keyof typeof REASONS]).toEqual(entry.reasons);
  });

  it('name a known reason in plain words and an unknown one with its category', () => {
    expect(reasonKey('needs_decision', 'question')).toBe('attention.reason.question');
    expect(reasonKey('done', 'question')).toBe('attention.generic.done');
    expect(reasonKey('blocked', 'later_reason')).toBe('attention.generic.blocked');
  });

  it('name the time a wait for model quota continues (ADR-081 section 5)', () => {
    const options = { locale: 'en' as const, timeZone: 'UTC', now: Date.parse('2026-11-24T08:00:00.000Z') };
    const until = '2026-11-24T14:05:00.000Z';
    expect(reasonText('working', { reason: 'quota_wait', wait_until: until }, options)).toEqual({
      key: 'attention.reason.quota_wait.at',
      params: { time: '14:05' },
    });
    expect(reasonText('needs_decision', { reason: 'quota_choice', wait_until: until }, options)).toEqual({
      key: 'attention.reason.quota_choice.at',
      params: { time: '14:05' },
    });
    // without a time, without the field, another reason, or a timed reason in the wrong category
    expect(reasonText('working', { reason: 'quota_wait', wait_until: null }, options)).toEqual({
      key: 'attention.reason.quota_wait',
      params: {},
    });
    expect(reasonText('needs_decision', { reason: 'quota_choice' }, options).key).toBe('attention.reason.quota_choice');
    expect(reasonText('working', { reason: 'working', wait_until: until }, options).key).toBe(
      'attention.reason.working',
    );
    expect(reasonText('blocked', { reason: 'quota_wait', wait_until: until }, options)).toEqual({
      key: 'attention.generic.blocked',
      params: {},
    });
  });

  it('offer the next step, "Try again" only where a new attempt can help', () => {
    const action = (category: string, reason: string) => actionOf({ category, reason });
    expect(action('needs_decision', 'question')).toBe('answer');
    expect(action('needs_decision', 'approval')).toBe('approve');
    expect(action('needs_decision', 'connection_missing')).toBe('connect');
    expect(action('needs_decision', 'device_missing')).toBe('assign');
    expect(action('needs_decision', 'credit_empty')).toBe('continue');
    expect(action('needs_decision', 'quota_choice')).toBe('consent');
    expect(action('working', 'quota_wait')).toBeNull();
    expect(action('needs_decision', 'later')).toBeNull();
    expect(action('blocked', 'model_access_missing')).toBe('model');
    for (const reason of ['session_failed', 'guest_error', 'device_unreachable', 'no_capacity'])
      expect(action('blocked', reason)).toBe('retry');
    expect(action('blocked', 'operation_refused')).toBeNull();
    expect(action('working', 'working')).toBeNull();
    expect(action('done', 'idle')).toBeNull();
  });

  it('shows a badge count up to 99', () => {
    expect(badgeText(7)).toBe('7');
    expect(badgeText(99)).toBe('99');
    expect(badgeText(100)).toBe('99+');
  });

  it('start an approval with exactly "Approved." or "Declined.", then a blank line and the comment', () => {
    expect(approvalText(true, '')).toBe('Approved.');
    expect(approvalText(false, '   ')).toBe('Declined.');
    expect(approvalText(true, ' Go ahead \n')).toBe('Approved.\n\nGo ahead');
    expect(approvalText(false, 'Not now')).toBe('Declined.\n\nNot now');
  });
});

describe('question', () => {
  let seq = 1;
  const params = (
    body: unknown,
    options: { status?: EventEntry['status']; author?: string; ref?: string } = {},
  ): EventEntry => {
    const status = options.status ?? 'verified';
    const header = {
      id: `e${seq}`,
      kind: 'operation.params',
      ref: options.ref ?? OP,
      author: options.author ?? DEVICE,
      ts: '2026-11-21T12:00:00.000Z',
    };
    return {
      org: 'o',
      scopeId: 'w',
      scopeSeq: seq++,
      receivedAt: null,
      event: { header },
      status,
      code: status === 'unverifiable' ? 'signature_invalid' : null,
      header: status === 'verified' ? header : null,
      body: status === 'verified' ? body : null,
    };
  };
  const asked = (type: string, question: unknown) => ({ operation_id: OP, type, params: { question } });

  const operation = (type: string, task = 't') => ({
    operation_id: OP,
    org_id: 'o',
    wg_id: 'w',
    task_id: task,
    session_id: 's',
    device_id: DEVICE,
    actor_id: DEVICE,
    type,
    state: 'succeeded',
    error_code: null,
    retryable: false,
    duplicate_of: null,
    attempt: 1,
    params_event_id: null,
    result_event_id: null,
    created_at: '2026-11-21T12:00:00.000Z',
    decided_at: null,
    started_at: null,
    finished_at: null,
  });

  it('is the verified question of the asking operation of the task, else unverifiable or missing', () => {
    const store = createStore();
    const row = record('t');
    const select = selectQuestion('w', row);
    expect(select(store.getState())).toEqual({ status: 'missing' });
    expect(select(store.getState())).toBe(select(store.getState()));
    putEvents(store, 'w', [params(null, { status: 'unverifiable' })]);
    expect(select(store.getState())).toEqual({ status: 'unverifiable', code: 'signature_invalid' });
    putEvents(store, 'w', [params(asked('human.question', 'Which branch?'))]);
    // verified parameters wait for the hub's row (its device must be their author)
    expect(select(store.getState())).toEqual({ status: 'missing' });
    putOperations(store, [operation('human.question')]);
    expect(select(store.getState())).toEqual({ status: 'verified', text: 'Which branch?' });
    // another type than the record's reason, an empty question, a row of another type or task is not this question
    const cases: [unknown, string, string][] = [
      [asked('human.approval', 'Deploy?'), 'human.question', 't'],
      [asked('human.question', ''), 'human.question', 't'],
      [asked('human.question', 5), 'human.question', 't'],
      [asked('human.question', 'Which branch?'), 'human.approval', 't'],
      [asked('human.question', 'Which branch?'), 'human.question', 'other'],
    ];
    for (const [body, type, task] of cases) {
      const other = createStore();
      putEvents(other, 'w', [params(body)]);
      putOperations(other, [operation(type, task)]);
      expect(selectQuestion('w', row)(other.getState())).toEqual({ status: 'unverifiable', code: 'params_invalid' });
    }
    const typed = createStore();
    putEvents(typed, 'w', [params(asked('human.approval', 'Deploy?'))]);
    putOperations(typed, [operation('human.approval')]);
    expect(selectQuestion('w', record('t', { reason: 'approval' }))(typed.getState())).toEqual({
      status: 'verified',
      text: 'Deploy?',
    });
  });

  it('is never read for other reasons or without an operation', () => {
    const store = createStore();
    putEvents(store, 'w', [params(asked('human.question', 'Which branch?'))]);
    for (const row of [
      record('t', { reason: 'connection_missing' }),
      record('t', { ref_kind: 'session' }),
      record('t', { ref_id: null }),
    ])
      expect(selectQuestion('w', row)(store.getState())).toEqual({ status: 'missing' });
  });
});

describe('window', () => {
  const ids = Array.from({ length: 120 }, (_, index) => `t${index}`);

  it('shows at most WINDOW entries from the start entry, the top when none', () => {
    expect(groupWindow(ids, null)).toEqual({ from: 0, to: WINDOW });
    expect(groupWindow(ids, 't30')).toEqual({ from: 30, to: 30 + WINDOW });
    // never beyond the last full window
    expect(groupWindow(ids, 't110')).toEqual({ from: 120 - WINDOW, to: 120 });
    // an entry that left keeps the window near its old place
    expect(groupWindow(ids, 'gone', 40)).toEqual({ from: 40, to: 40 + WINDOW });
    expect(groupWindow(['a'], null)).toEqual({ from: 0, to: 1 });
    expect(groupWindow([], 'x', 5)).toEqual({ from: 0, to: 0 });
  });

  it('moves by STEP entries and starts at the top as null', () => {
    expect(laterFrom(ids, 0)).toBe(STEP);
    expect(laterFrom(ids, 60)).toBe(120 - WINDOW);
    expect(laterFrom(ids.slice(0, 10), 0)).toBe(0);
    expect(earlierFrom(60)).toBe(60 - STEP);
    expect(earlierFrom(STEP - 1)).toBe(0);
    expect(windowAt(ids, 0)).toEqual({ start: null, from: 0 });
    expect(windowAt(ids, 30)).toEqual({ start: 't30', from: 30 });
  });
});

describe('selectors', () => {
  const T = { org: 'o', wg: 'w', me: { id: 'u' } } as never;

  it('give the known task rows of the shown entries (the same array while the rows are unchanged)', () => {
    const store = createStore();
    const select = selectTaskRows(['a', 'b']);
    expect(select(store.getState())).toEqual([]);
    putTasks(store, [taskRow(T, { task_id: 'a' }) as TaskRow]);
    const rows = select(store.getState());
    expect(rows.map((row) => row.task_id)).toEqual(['a']);
    expect(select(store.getState())).toBe(rows);
  });

  it('know an org without any entry once every group answered', () => {
    const store = createStore();
    const empty = selectNoTask('o');
    expect(empty(store.getState())).toBe(false);
    for (const category of CATEGORIES)
      groupPage(store, { org: 'o', category, rows: [], nextBefore: null, first: true });
    expect(empty(store.getState())).toBe(true);
    groupPage(store, {
      org: 'o',
      category: 'done',
      rows: [record('a', { category: 'done' })],
      nextBefore: null,
      first: true,
    });
    expect(empty(store.getState())).toBe(false);
  });
});
