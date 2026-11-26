// The situation view by person (06-web.md section 4.0a, ADR-095 section 5): one row per person with open main agents
// in the workgroups the user reads (`GET /v1/orgs/{org}/people/agents`, 50 at a time, again after a reconnect) – the
// person's name (the signed directory), main agents, waiting for a decision, stuck, running, active subagents – the
// user's own row first ("You"), the others of the loaded pages by name; at most 50 rows in the DOM. A row opens the
// person's agents (`AgentList.tsx`). Works on a phone: one column, controls at least 44 px.
import { memo, useEffect, useMemo } from 'react';
import { loadPeople, orderPeople } from '../../../../core/agents/people';
import { formatNumber } from '../../../../core/i18n';
import type { OrgState } from '../../../../core/log/state';
import { peopleList, personOpen, setPersonOpen } from '../../../../core/agents/state';
import type { PersonRow as Person } from '../../../../core/store/slices/mainAgents';
import type { Directory } from '../../../../core/tasks/directory';
import { useLiveReload } from '../../../hooks/useLiveReload';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { personName } from '../../../names/names';
import { useIdentities } from '../../../settings/sections/github/identities';
import { useAppEnv } from '../../../shell/context';
import { AgentList } from './AgentList';
import { useWindowList } from './useWindowList';

const LINK = 'min-h-11 self-start py-2 text-sm text-blue-800 underline';

/** The counters of a person row: test ID suffix and field. */
const COUNTS: readonly [string, keyof Person][] = [
  ['agents', 'agents'],
  ['needs_decision', 'needs_decision'],
  ['blocked', 'blocked'],
  ['working', 'working'],
  ['subagents', 'subagents_active'],
];

interface RowProps {
  readonly org: string;
  readonly person: Person;
  readonly name: string;
  readonly state: OrgState;
  readonly userId: string;
  readonly directory: Directory;
  readonly now: number;
}

function Row({ org, person, name, state, userId, directory, now }: RowProps) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  // in the store: a refresh, a moved window or a remount of the view keeps the row open
  const open = useStore((store) => personOpen(store.mainAgents, org, person.owner_id), app.store);
  const setOpen = (next: boolean) => setPersonOpen(app.store, org, person.owner_id, next);
  return (
    <li
      className="flex flex-col gap-2 border-b border-gray-100 px-2 py-2"
      data-testid="agents.person"
      data-owner={person.owner_id}
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
        <span className="font-medium" data-testid="agents.person.name">
          {name}
        </span>
        <ul className="flex flex-wrap gap-x-3 gap-y-1 text-sm text-gray-700">
          {COUNTS.map(([suffix, field]) => (
            <li key={suffix}>
              <span className="font-semibold text-gray-900" data-testid={`agents.person.count.${suffix}`}>
                {formatNumber(person[field] as number, locale)}
              </span>{' '}
              {t(`mainagent.people.count.${suffix}`)}
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="min-h-11 self-start rounded border border-gray-300 px-3 text-sm"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          data-testid="agents.person.toggle"
        >
          {open ? t('mainagent.people.toggle.hide') : t('mainagent.people.toggle.show')}
        </button>
      </div>
      {open && (
        <AgentList
          org={org}
          owner={person.owner_id}
          person={name}
          state={state}
          userId={userId}
          directory={directory}
          now={now}
        />
      )}
    </li>
  );
}

/** A row renders again only when its own props change. */
const PersonRow = memo(Row);

export interface PeopleViewProps {
  readonly org: string;
  readonly state: OrgState;
  readonly userId: string;
  readonly directory: Directory;
  readonly now: number;
}

export function PeopleView({ org, state, userId, directory, now }: PeopleViewProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  // the GitHub identities the agents act as are named from the user's identity records
  useIdentities();
  const people = useStore((store) => peopleList(store.mainAgents, org), app.store);
  useEffect(() => {
    void loadPeople(app.tasks, org);
  }, [app, org]);
  useLiveReload(app.store, () => void loadPeople(app.tasks, org, false, true));
  const nameOf = useMemo(
    () => (owner: string) => (owner === userId ? t('mainagent.people.you') : personName(t, directory, owner)),
    [t, directory, userId],
  );
  const order = useMemo(() => orderPeople(people.owners, userId, nameOf), [people.owners, userId, nameOf]);
  const { span, end, later, more, earlier } = useWindowList(
    order,
    people.nextAfter !== null,
    () => loadPeople(app.tasks, org, true),
    () => orderPeople(peopleList(app.store.getState().mainAgents, org).owners, userId, nameOf),
  );
  return (
    <section
      className="flex flex-col gap-2"
      aria-labelledby="agents-people"
      data-testid="agents.people"
      data-total={order.length}
      data-from={span.from}
    >
      <h2 id="agents-people" className="font-semibold">
        {t('mainagent.people.title')}
      </h2>
      {people.code !== null && (
        <div role="alert" className="flex flex-wrap items-center gap-2 text-red-800" data-testid="agents.people.error">
          <span>{error(people.code).title}</span>
          <button
            type="button"
            className="min-h-11 underline"
            onClick={() => void loadPeople(app.tasks, org, order.length > 0)}
            data-testid="agents.people.reload"
          >
            {t('attention.reload')}
          </button>
        </div>
      )}
      {(people.status === 'idle' || (people.status === 'loading' && order.length === 0)) && (
        <p className="text-sm text-gray-600" data-testid="agents.people.loading">
          {t('mainagent.people.loading')}
        </p>
      )}
      {people.status === 'ready' && order.length === 0 && (
        <p className="text-sm text-gray-600" data-testid="agents.people.empty">
          {t('mainagent.people.empty')}
        </p>
      )}
      {span.from > 0 && (
        <button type="button" className={LINK} onClick={earlier} data-testid="agents.people.earlier">
          {t('mainagent.people.earlier')}
        </button>
      )}
      {order.length > 0 && (
        <ul className="flex flex-col" data-testid="agents.people.list" aria-busy={people.status === 'loading'}>
          {order.slice(span.from, span.to).map((owner) => (
            <PersonRow
              key={owner}
              org={org}
              person={people.rows[owner]!}
              name={nameOf(owner)}
              state={state}
              userId={userId}
              directory={directory}
              now={now}
            />
          ))}
          {later && <li ref={end} aria-hidden="true" data-testid="agents.people.end" />}
        </ul>
      )}
      {later && (
        <button type="button" className={LINK} onClick={() => void more()} data-testid="agents.people.more">
          {span.to < order.length ? t('mainagent.people.later') : t('mainagent.people.more')}
        </button>
      )}
    </section>
  );
}
