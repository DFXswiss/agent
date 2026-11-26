// The open main agents of one person (06-web.md section 4.0a, ADR-095 section 5): loaded from the hub when the person's
// row opens (`GET /v1/orgs/{org}/main-agents?owner=…`, 50 at a time, again after a reconnect), newest activity first,
// windowed (at most 50 in the DOM). The titles of the shown agents are pulled once the window rests for `TITLES_MS`.
import { useEffect, useMemo } from 'react';
import { loadMainAgents, selectAgentIds } from '../../../../core/agents/people';
import { selectTaskRows } from '../../../../core/attention/view';
import type { OrgState } from '../../../../core/log/state';
import { agentList } from '../../../../core/agents/state';
import type { Directory } from '../../../../core/tasks/directory';
import { titles } from '../../../../core/tasks/list';
import { useLiveReload } from '../../../hooks/useLiveReload';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { TITLES_MS } from '../AttentionGroup';
import { AgentItem } from './AgentItem';
import { useWindowList } from './useWindowList';

const LINK = 'min-h-11 self-start py-2 text-sm text-blue-800 underline';

export interface AgentListProps {
  readonly org: string;
  readonly owner: string;
  /** The shown name of the person (the list's label). */
  readonly person: string;
  readonly state: OrgState;
  readonly userId: string;
  readonly directory: Directory;
  readonly now: number;
}

export function AgentList({ org, owner, person, state, userId, directory, now }: AgentListProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const list = useStore((store) => agentList(store.mainAgents, org, owner), app.store);
  const select = useMemo(() => selectAgentIds(org, owner), [org, owner]);
  const ids = useStore(select, app.store);
  const { span, end, later, more, earlier } = useWindowList(
    ids,
    list.nextBefore !== null,
    () => loadMainAgents(app.tasks, org, owner, true),
    () => select(app.store.getState()),
  );
  const shownKey = ids.slice(span.from, span.to).join(' ');
  const selectRows = useMemo(() => selectTaskRows(shownKey === '' ? [] : shownKey.split(' ')), [shownKey]);
  const rows = useStore(selectRows, app.store);
  useEffect(() => {
    if (rows.length === 0) return;
    const timer = setTimeout(() => void titles(app.tasks, rows), TITLES_MS);
    return () => clearTimeout(timer);
  }, [app, rows]);
  useEffect(() => {
    void loadMainAgents(app.tasks, org, owner);
  }, [app, org, owner]);
  useLiveReload(app.store, () => void loadMainAgents(app.tasks, org, owner, false, true));
  return (
    <div
      className="flex flex-col gap-2 sm:pl-4"
      data-testid="agents.list"
      data-owner={owner}
      data-total={ids.length}
      data-from={span.from}
    >
      {list.code !== null && (
        <div role="alert" className="flex flex-wrap items-center gap-2 text-red-800" data-testid="agents.error">
          <span>{error(list.code).title}</span>
          <button
            type="button"
            className="min-h-11 underline"
            onClick={() => void loadMainAgents(app.tasks, org, owner, ids.length > 0)}
            data-testid="agents.reload"
          >
            {t('attention.reload')}
          </button>
        </div>
      )}
      {(list.status === 'idle' || (list.status === 'loading' && ids.length === 0)) && (
        <p className="text-sm text-gray-600" data-testid="agents.loading">
          {t('mainagent.agents.loading')}
        </p>
      )}
      {list.status === 'ready' && ids.length === 0 && (
        <p className="text-sm text-gray-600" data-testid="agents.empty">
          {t('mainagent.agents.empty')}
        </p>
      )}
      {span.from > 0 && (
        <button type="button" className={LINK} onClick={earlier} data-testid="agents.earlier">
          {t('mainagent.agents.earlier')}
        </button>
      )}
      {ids.length > 0 && (
        <ul
          className="flex flex-col"
          aria-label={t('mainagent.agents.label', { person })}
          aria-busy={list.status === 'loading'}
        >
          {ids.slice(span.from, span.to).map((id) => (
            <AgentItem key={id} org={org} id={id} state={state} userId={userId} directory={directory} now={now} />
          ))}
          {later && <li ref={end} aria-hidden="true" data-testid="agents.end" />}
        </ul>
      )}
      {later && (
        <button type="button" className={LINK} onClick={() => void more()} data-testid="agents.more">
          {span.to < ids.length ? t('mainagent.agents.later') : t('mainagent.agents.more')}
        </button>
      )}
    </div>
  );
}
