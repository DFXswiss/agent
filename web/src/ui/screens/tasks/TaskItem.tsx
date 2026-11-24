// One row of a task list (06-web.md sections 5.1, 12.4): the decrypted title (or the neutral "unverifiable" label
// with its code), the workgroup in lists of several workgroups (KOR-32), the state badge, the owner's display name (the
// signed author of the task's first user-written `task.meta`; the hub's `owner_id` is ignored, KOR-04) and the last
// activity as absolute local time with a relative label as secondary text.
//
// The row is the content of a `WindowedList` row (ADR-063 section 1): everything it shows comes in its item (the list
// reads titles and owners with one selector), it is memoized with `sameTaskItem`, and the time formats are created
// once per locale (module level), never per row.
import { Link } from 'react-router-dom';
import { parseTime } from '../../../core/encoding/time';
import { formatEventTime, localTimeZone } from '../../../core/i18n';
import type { TaskRow } from '../../../core/store/slices/tasks';
import type { Title } from '../../../core/tasks/directory';
import { useI18n } from '../../i18n';
import type { RowAttributes, RowProps } from '../../lists/WindowedList';
import { sameTitle } from './hooks';

const BADGE: Readonly<Record<string, string>> = {
  scheduling: 'bg-amber-100 text-amber-900',
  starting: 'bg-blue-100 text-blue-900',
  working: 'bg-green-100 text-green-900',
  failed: 'bg-red-100 text-red-900',
};

const UNITS: readonly [Intl.RelativeTimeFormatUnit, number][] = [
  ['day', 86_400_000],
  ['hour', 3_600_000],
  ['minute', 60_000],
];

const relativeFormats = new Map<string, Intl.RelativeTimeFormat>();

function relativeFormat(locale: string): Intl.RelativeTimeFormat {
  let format = relativeFormats.get(locale);
  if (format === undefined) {
    format = new Intl.RelativeTimeFormat(locale, { numeric: 'auto', style: 'short' });
    relativeFormats.set(locale, format);
  }
  return format;
}

/** "2 min ago" style label (seconds below one minute). */
export function relativeLabel(locale: string, ms: number): string {
  const format = relativeFormat(locale);
  const past = Math.max(0, ms);
  for (const [unit, size] of UNITS) if (past >= size) return format.format(-Math.floor(past / size), unit);
  return format.format(-Math.floor(past / 1000), 'second');
}

/** Everything one row shows. */
export interface TaskItemData {
  readonly org: string;
  readonly row: TaskRow;
  readonly title: Title;
  /** The owner's display name, null when unknown. */
  readonly owner: string | null;
  /** The name of the row's workgroup, shown in lists of several workgroups. */
  readonly workgroup: string | null;
  readonly now: number;
}

export const taskKey = (item: TaskItemData): string => item.row.task_id;

/** Whether two builds of a row render the same (the row object changes with every update of the task). */
export const sameTaskItem = (a: TaskItemData, b: TaskItemData): boolean =>
  a.row === b.row &&
  a.org === b.org &&
  a.owner === b.owner &&
  a.workgroup === b.workgroup &&
  a.now === b.now &&
  sameTitle(a.title, b.title);

export const taskRowAttributes = (item: TaskItemData): RowAttributes => ({
  className: 'border-b border-gray-100',
  'data-testid': 'tasks.row',
  'data-task': item.row.task_id,
});

export function TaskItem({ item }: RowProps<TaskItemData>) {
  const { t, locale } = useI18n();
  const { org, row, title, owner, workgroup, now } = item;
  const activity = parseTime(row.last_activity_at);
  return (
    <Link
      className="flex min-h-11 flex-col gap-1 px-2 py-2 sm:flex-row sm:items-center sm:gap-3"
      to={`/o/${encodeURIComponent(org)}/t/${encodeURIComponent(row.task_id)}`}
    >
      <span className="flex-1 font-medium" data-testid="tasks.row.title" data-status={title.status}>
        {title.status === 'verified' && title.text}
        {title.status === 'missing' && <span className="text-gray-500">{t('tasks.title.loading')}</span>}
        {title.status === 'unverifiable' && (
          <span className="text-gray-500">{t('tasks.title.unverifiable', { code: title.code })}</span>
        )}
      </span>
      {workgroup !== null && (
        <span className="text-sm text-gray-700" data-testid="tasks.row.wg" data-wg={row.wg_id}>
          {workgroup}
        </span>
      )}
      <span
        className={`self-start rounded px-2 text-xs ${BADGE[row.state] ?? 'bg-gray-100 text-gray-800'}`}
        data-testid="tasks.row.state"
        data-state={row.state}
      >
        {t(`tasks.state.${row.state}`)}
      </span>
      <span className="text-sm text-gray-700" data-testid="tasks.row.owner">
        {owner ?? t('tasks.owner.unknown')}
      </span>
      <span className="text-xs text-gray-600">
        <time dateTime={row.last_activity_at} data-testid="tasks.row.activity">
          {formatEventTime(activity, { locale, timeZone: localTimeZone(), now })}
        </time>{' '}
        <span>({relativeLabel(locale, now - activity)})</span>
      </span>
    </Link>
  );
}
