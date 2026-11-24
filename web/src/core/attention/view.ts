// What the situation view shows and offers (06-web.md section 4.0, ADR-076 sections 1 and 8):
//
// - the reasons of each category (closed lists of `contracts/enums/attention.json`); an unknown reason is shown with
//   its category's generic text (a newer hub may add reasons);
// - the next step of an entry (`actionOf`): an answer field, approve / decline, connect GitHub, assign a computer,
//   the model settings, "Continue" after an empty balance, "Try again" only where a new attempt can help, nothing
//   else for a refused action ("Open task" is always offered);
// - the prompt of an approval: exactly `Approved.` or `Declined.` (English in every locale, so the agent reads the
//   same), then a blank line and the comment when one was given;
// - the agent's question of a `question` / `approval` record: the `params.question` of the verified
//   `operation.params` event of the record's operation, and only for the two asking operation types;
// - the window of a group (`groupWindow`): at most `WINDOW` entries in the DOM, moved by `STEP` (HOT-05).
import { selectOperation } from '../operations/view';
import { type AttentionRow, attentionGroup, CATEGORIES, type Category } from '../store/slices/attention';
import type { TaskRow } from '../store/slices/tasks';
import type { StoreState } from '../store/store';

/** The reasons of each category, in the order of ADR-076 section 1. */
export const REASONS: Readonly<Record<Category, readonly string[]>> = {
  needs_decision: ['question', 'approval', 'connection_missing', 'credit_empty', 'device_missing'],
  blocked: [
    'session_failed',
    'guest_error',
    'device_unreachable',
    'model_access_missing',
    'no_capacity',
    'operation_refused',
  ],
  working: ['queued', 'starting', 'working', 'operation', 'subagents'],
  done: ['idle', 'closed'],
};

/** The message key of a reason's plain words (`attention` catalog). */
export function reasonKey(category: Category, reason: string): string {
  return REASONS[category].includes(reason) ? `attention.reason.${reason}` : `attention.generic.${category}`;
}

/** The operation types with which an agent asks a person (03-api.md section 9). */
export const ASKING_TYPES: Readonly<Record<string, string>> = {
  question: 'human.question',
  approval: 'human.approval',
};

/** Reasons after which a new attempt (the newest prompt again, a new main session) can help. */
export const RETRY_REASONS: ReadonlySet<string> = new Set([
  'session_failed',
  'guest_error',
  'device_unreachable',
  'no_capacity',
]);

export type Action = 'answer' | 'approve' | 'connect' | 'assign' | 'model' | 'continue' | 'retry' | null;

/** The next step of a record (module comment). */
export function actionOf(row: Pick<AttentionRow, 'category' | 'reason'>): Action {
  if (row.category === 'needs_decision') {
    switch (row.reason) {
      case 'question':
        return 'answer';
      case 'approval':
        return 'approve';
      case 'connection_missing':
        return 'connect';
      case 'device_missing':
        return 'assign';
      case 'credit_empty':
        return 'continue';
      default:
        return null;
    }
  }
  if (row.category === 'blocked') {
    if (row.reason === 'model_access_missing') return 'model';
    return RETRY_REASONS.has(row.reason) ? 'retry' : null;
  }
  return null;
}

export const APPROVED = 'Approved.';
export const DECLINED = 'Declined.';
/** The prompt that continues after an empty balance was set again. */
export const CONTINUE = 'Continue.';

/** The prompt of an approval (module comment). */
export function approvalText(approve: boolean, comment: string): string {
  const head = approve ? APPROVED : DECLINED;
  const text = comment.trim();
  return text === '' ? head : `${head}\n\n${text}`;
}

/** The agent's question: `verified` with the text, `unverifiable` with the code, or `missing` (not read yet). */
export type Question =
  | { readonly status: 'verified'; readonly text: string }
  | { readonly status: 'unverifiable'; readonly code: string }
  | { readonly status: 'missing' };

const MISSING: Question = { status: 'missing' };

/** A memoized selector of the question behind a record (module comment). */
export function selectQuestion(wg: string, row: Pick<AttentionRow, 'task_id' | 'reason' | 'ref_kind' | 'ref_id'>) {
  const type = ASKING_TYPES[row.reason];
  if (type === undefined || row.ref_kind !== 'operation' || row.ref_id === null) return (): Question => MISSING;
  const operation = selectOperation(wg, row.ref_id);
  let last: { view: unknown; question: Question } | null = null;
  return (state: StoreState): Question => {
    const view = operation(state);
    if (last !== null && last.view === view) return last.question;
    let question: Question = MISSING;
    if (view.params.status === 'unverifiable') question = { status: 'unverifiable', code: view.params.code };
    // Shown only with the hub's row of the operation: its device must be the author of the sealed parameters (checked
    // by the operation view), and it must be an asking operation of this record's task.
    if (view.params.status === 'verified' && view.row !== null) {
      const text = view.params.value.params.question;
      const asked = view.params.value.type === type && view.row.type === type && view.row.task_id === row.task_id;
      question =
        asked && typeof text === 'string' && text !== ''
          ? { status: 'verified', text }
          : { status: 'unverifiable', code: 'params_invalid' };
    }
    last = { view, question };
    return question;
  };
}

/** Agents of an opened entry shown at most (the rest are counted, ADR-076 section 4). */
export const TREE_MAX = 100;

/** Entries of a group in the DOM at most (HOT-05: the window keeps the DOM small however long the group is). */
export const WINDOW = 50;
/** The window moves by this many entries. */
export const STEP = 25;

export interface GroupWindow {
  /** Index of the first rendered entry. */
  readonly from: number;
  /** Index after the last rendered entry. */
  readonly to: number;
}

/**
 * The window of a group: from the entry `start` (null: the top, so new entries at the top show) for `WINDOW`
 * entries. An entry that left the group keeps the window near its old place (`fallback`, the index it had).
 */
export function groupWindow(ids: readonly string[], start: string | null, fallback = 0): GroupWindow {
  const found = start === null ? 0 : ids.indexOf(start);
  const from = Math.max(0, Math.min(found < 0 ? fallback : found, ids.length - WINDOW));
  return { from, to: Math.min(ids.length, from + WINDOW) };
}

/** The first index after "Show later entries": `STEP` further, never beyond the last full window. */
export function laterFrom(ids: readonly string[], from: number): number {
  return Math.max(0, Math.min(from + STEP, ids.length - WINDOW));
}

/** The first index after "Show earlier entries": `STEP` back, at least the top. */
export function earlierFrom(from: number): number {
  return Math.max(0, from - STEP);
}

/** The window state that starts at index `from`: its first entry (null at the top, so new entries there show). */
export function windowAt(
  ids: readonly string[],
  from: number,
): { readonly start: string | null; readonly from: number } {
  return { start: from === 0 ? null : ids[from]!, from };
}

/**
 * A selector of the task rows of `ids` that are known (the shown entries, for their titles and owners); the same array
 * while the task rows are unchanged.
 */
export function selectTaskRows(ids: readonly string[]): (state: StoreState) => readonly TaskRow[] {
  let last: { tasks: unknown; rows: readonly TaskRow[] } | null = null;
  return (state) => {
    const tasks = state.tasks.tasks;
    if (last !== null && last.tasks === tasks) return last.rows;
    const rows = ids.flatMap((id) => (tasks[id] === undefined ? [] : [tasks[id]]));
    last = { tasks, rows };
    return rows;
  };
}

/** Whether every group of `org` answered and none has an entry (the organization shows no task). */
export const selectNoTask =
  (org: string) =>
  (state: StoreState): boolean =>
    CATEGORIES.every((category) => {
      const group = attentionGroup(state.attention, org, category);
      return group.status === 'ready' && group.ids.length === 0;
    });

/** The text of a count badge: the number, `99+` above 99. */
export const badgeText = (count: number): string => (count > 99 ? '99+' : String(count));
