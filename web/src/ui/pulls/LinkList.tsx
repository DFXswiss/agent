// The linked pull requests of a workgroup or of one task (KOR-48): loaded newest first (`core/pulls/links.ts`), one
// `PullRow` each, the load error with "Try again", the empty text, and "Load more" while the hub has further pages.
// Shared by the workgroup's "Pull requests" view and the task's panel.
import { useEffect, useMemo } from 'react';
import { loadLinks } from '../../core/pulls/links';
import { linkList, listKey, type PullLink } from '../../core/pulls/state';
import type { StoreState } from '../../core/store/store';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { PullRow } from './PullRow';

const BUTTON = 'min-h-11 self-start rounded border border-gray-300 px-4';

/** The links of a list in its order (the same array while neither the list nor the links changed). */
export function selectLinks(key: string) {
  let last: { list: unknown; links: unknown; items: readonly PullLink[] } | null = null;
  return (state: StoreState): readonly PullLink[] => {
    const list = linkList(state.pulls, key);
    if (last !== null && last.list === list && last.links === state.pulls.links) return last.items;
    // a listed ID always has its link (`removeLink` takes it out of every list)
    const items = list.ids.map((id) => state.pulls.links[id]!);
    last = { list, links: state.pulls.links, items };
    return items;
  };
}

export function LinkList({
  org,
  wg,
  task,
  writer,
}: {
  readonly org: string;
  readonly wg: string;
  /** Only the links of this task (the task's panel). */
  readonly task: string | null;
  readonly writer: boolean;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const key = listKey(wg, task);
  const list = useStore((state) => linkList(state.pulls, key), app.store);
  const select = useMemo(() => selectLinks(key), [key]);
  const items = useStore(select, app.store);
  useEffect(() => {
    void loadLinks(app.tasks, org, wg, task);
  }, [app, org, wg, task]);
  return (
    <div className="flex flex-col gap-2">
      {list.status === 'error' && (
        <div className="flex flex-col gap-2" role="alert" data-testid="pulls.error.list" data-code={list.code!}>
          <p className="text-sm text-red-800">{t('pulls.load-failed', { reason: error(list.code!).title })}</p>
          <button
            type="button"
            className={BUTTON}
            onClick={() => void loadLinks(app.tasks, org, wg, task)}
            data-testid="pulls.reload"
          >
            {t('pulls.reload')}
          </button>
        </div>
      )}
      {list.status === 'loading' && items.length === 0 && (
        <p className="text-sm text-gray-600" data-testid="pulls.loading">
          {t('pulls.loading')}
        </p>
      )}
      {list.status === 'ready' && items.length === 0 && (
        <p className="text-sm text-gray-700" data-testid="pulls.empty">
          {t(task === null ? 'pulls.empty' : 'pulls.empty.task')}
        </p>
      )}
      {items.length > 0 && (
        <ul className="flex flex-col" data-testid="pulls.list">
          {items.map((link) => (
            <PullRow key={`${link.epoch} ${link.tag}`} org={org} link={link} writer={writer} showTask={task === null} />
          ))}
        </ul>
      )}
      {list.status === 'ready' && list.nextCursor !== null && (
        <button
          type="button"
          className={BUTTON}
          onClick={() => void loadLinks(app.tasks, org, wg, task, true)}
          data-testid="pulls.more"
        >
          {t('pulls.more')}
        </button>
      )}
    </div>
  );
}
