// The compact pull-request line of a situation-view entry (KOR-48, ADR-084 section 3, 06-web.md section 4.0): an entry
// whose task has linked pull requests names each with its state and conflict state, and offers "Fix conflicts" right
// there for a conflict (users who may write in the task's workgroup). The workgroup's links are loaded once
// (`ensureLinks`); the states are read lazily – only for the entries on the screen (the window of a group), batched
// into one device request.
import { useEffect, useMemo, useState } from 'react';
import { pullLabel } from '../../../core/pulls/address';
import { fixConflicts } from '../../../core/pulls/fix';
import { ensureLinks } from '../../../core/pulls/links';
import { canFix } from '../../../core/pulls/view';
import { linkList, listKey, type PullLink } from '../../../core/pulls/state';
import type { TaskRow } from '../../../core/store/slices/tasks';
import type { StoreState } from '../../../core/store/store';
import { sendErrorCode } from '../../../core/tasks/send';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { usePull } from '../../pulls/hooks';
import { useAppEnv } from '../../shell/context';

type Status = { kind: 'idle' } | { kind: 'sending' } | { kind: 'sent' } | { kind: 'error'; code: string };

/** The loaded links of `task` (the same array while the workgroup's list and the links are unchanged). */
export function selectTaskLinks(wg: string, task: string) {
  let last: { list: unknown; links: unknown; items: readonly PullLink[] } | null = null;
  return (state: StoreState): readonly PullLink[] => {
    const list = linkList(state.pulls, listKey(wg, null));
    if (last !== null && last.list === list && last.links === state.pulls.links) return last.items;
    const items = list.ids.map((id) => state.pulls.links[id]!).filter((link) => link.taskId === task);
    last = { list, links: state.pulls.links, items };
    return items;
  };
}

function PullEntry({ org, link, writer }: { readonly org: string; readonly link: PullLink; readonly writer: boolean }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const { pull, wg, taskId: task } = link;
  const { display } = usePull({ org, wg, task }, pull);
  const [status, setStatus] = useState<Status>({ kind: 'idle' });
  const state = display.kind === 'found' ? display.state : display.kind;
  const conflict = display.kind === 'found' ? display.conflict : null;
  const fix = async () => {
    setStatus({ kind: 'sending' });
    try {
      await fixConflicts(app.tasks, task, pull!);
      setStatus({ kind: 'sent' });
    } catch (failure) {
      setStatus({ kind: 'error', code: sendErrorCode(failure) });
    }
  };
  return (
    <li
      className="flex flex-col gap-1 sm:flex-row sm:flex-wrap sm:items-center sm:gap-x-3"
      data-testid="attention.pull"
      data-pull={pull === null ? '' : pullLabel(pull)}
      data-state={state}
      data-conflict={conflict ?? ''}
    >
      <span className="break-all font-mono">{pull === null ? t('pulls.unavailable') : pullLabel(pull)}</span>
      {pull !== null && (
        <span className="text-gray-700" data-testid="attention.pull.state">
          {display.kind === 'failed' ? t('pulls.state.failed.short') : t(`pulls.state.${state}`)}
          {conflict !== null && ` · ${t(`pulls.conflict.${conflict}`)}`}
        </span>
      )}
      {canFix(display, writer) && status.kind !== 'sent' && (
        <button
          type="button"
          className="min-h-11 self-start rounded bg-blue-700 px-4 text-white disabled:opacity-50"
          disabled={status.kind === 'sending'}
          onClick={() => void fix()}
          data-testid="attention.fix-conflicts"
        >
          {t('pulls.fix')}
        </button>
      )}
      {status.kind === 'sent' && (
        <span role="status" className="text-green-800" data-testid="attention.fix-conflicts.sent">
          {t('pulls.fix.sent')}
        </span>
      )}
      {status.kind === 'error' && (
        <span role="alert" className="text-red-800" data-testid="attention.fix-conflicts.error" data-code={status.code}>
          {error(status.code).title}
        </span>
      )}
    </li>
  );
}

export function PullLine({
  org,
  task,
  writer,
}: {
  readonly org: string;
  readonly task: TaskRow;
  readonly writer: boolean;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const select = useMemo(() => selectTaskLinks(task.wg_id, task.task_id), [task.wg_id, task.task_id]);
  const links = useStore(select, app.store);
  useEffect(() => {
    void ensureLinks(app.tasks, org, task.wg_id);
  }, [app, org, task.wg_id]);
  if (links.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1 px-2 pb-2 text-sm" aria-label={t('pulls.tab')} data-testid="attention.pulls">
      {links.map((link) => (
        <PullEntry key={`${link.epoch} ${link.tag}`} org={org} link={link} writer={writer} />
      ))}
    </ul>
  );
}
