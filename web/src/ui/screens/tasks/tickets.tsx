// Shared parts of the captured tasks (E-41, ADR-100 section 7): the topics of the org (pulled once), the people a task
// may name (members and agent accounts of the verified log, named by the directory), the suggestions for GitHub
// organisation and repository (the logins of the user's GitHub identities, the workgroups' repository lists; free input
// stays allowed), and the badges of status and urgency shown by rows and the task page.
import { useEffect, useMemo } from 'react';
import { KIND as SETTINGS_KIND, selectCurrent, settingsOf } from '../../../core/wg-settings/settings';
import type { TaskRow } from '../../../core/store/slices/tasks';
import type { StoreState } from '../../../core/store/store';
import { pullOnce } from '../../../core/tasks/pull';
import {
  assignablePeople,
  loadTopics,
  selectTopics,
  taskStatus,
  taskUrgency,
  type Topic,
} from '../../../core/tasks/tickets';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { personName } from '../../names/names';
import { useIdentities } from '../../settings/sections/github/identities';
import { useAppEnv } from '../../shell/context';
import { useDirectory, useOrgView } from './hooks';

/** The topics of `org` by name; pulls them once. */
export function useTopics(org: string): readonly Topic[] {
  const { app } = useAppEnv();
  useEffect(() => {
    loadTopics(app.tasks, org).catch(() => 0);
  }, [app, org]);
  return useStore(useMemo(() => selectTopics(org), [org]), app.store);
}

/** A person a task may name: ID and shown name. */
export interface Person {
  readonly id: string;
  readonly name: string;
}

/** The members and agent accounts of `org` (empty until the log is verified). */
export function usePeople(org: string): readonly Person[] {
  const { t } = useI18n();
  const { state } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  return useMemo(
    () => (state === null ? [] : assignablePeople(state).map((id) => ({ id, name: personName(t, directory, id) }))),
    [state, directory, t],
  );
}

/** Suggestions for the GitHub organisation and repository of a task of the workgroups `wgs`. */
export interface GithubSuggestions {
  readonly orgs: readonly string[];
  readonly repos: readonly string[];
}

const unique = (values: readonly string[]): string[] =>
  [...new Map(values.map((value) => [value.toLowerCase(), value])).values()].sort((a, b) => a.localeCompare(b));

/** The suggestions (module comment); pulls the workgroups' settings once. */
export function useGithubSuggestions(org: string, wgs: readonly string[]): GithubSuggestions {
  const { app } = useAppEnv();
  const { state: identities } = useIdentities();
  const key = wgs.join(',');
  useEffect(() => {
    for (const wg of key === '' ? [] : key.split(',')) {
      pullOnce(app.tasks, org, wg, { kinds: [SETTINGS_KIND] }).catch(() => 0);
    }
  }, [app, org, key]);
  const repos = useStore(
    useMemo(() => {
      let last: string[] = [];
      return (store: StoreState) => {
        const next = unique(
          (key === '' ? [] : key.split(',')).flatMap((wg) => settingsOf(selectCurrent(wg)(store)).repositories),
        );
        if (next.join() !== last.join()) last = next;
        return last;
      };
    }, [key]),
    app.store,
  );
  return useMemo(() => {
    const logins = [
      ...identities.identities.flatMap((item) => (item.status === 'verified' ? [item.account.login] : [])),
      ...Object.values(identities.knownLogins),
    ];
    return { orgs: unique([...logins, ...repos.map((repo) => repo.split('/')[0]!)]), repos };
  }, [identities, repos]);
}

const STATUS_BADGE: Readonly<Record<string, string>> = {
  todo: 'bg-gray-100 text-gray-900',
  inProgress: 'bg-blue-100 text-blue-900',
  completed: 'bg-green-100 text-green-900',
};

const URGENCY_BADGE: Readonly<Record<string, string>> = {
  low: 'bg-gray-100 text-gray-800',
  normal: 'bg-gray-100 text-gray-800',
  high: 'bg-amber-100 text-amber-900',
  urgent: 'bg-red-100 text-red-900',
};

/** The status of a task as a badge. */
export function StatusBadge({ row, testId }: { readonly row: TaskRow; readonly testId: string }) {
  const { t } = useI18n();
  const status = taskStatus(row);
  return (
    <span className={`self-start rounded px-2 text-xs ${STATUS_BADGE[status]}`} data-testid={testId} data-status={status}>
      {t(`tickets.status.${status}`)}
    </span>
  );
}

/** The urgency of a task as a badge. */
export function UrgencyBadge({ row, testId }: { readonly row: TaskRow; readonly testId: string }) {
  const { t } = useI18n();
  const urgency = taskUrgency(row);
  return (
    <span
      className={`self-start rounded px-2 text-xs ${URGENCY_BADGE[urgency]}`}
      data-testid={testId}
      data-urgency={urgency}
    >
      {t('tickets.row.urgency', { urgency: t(`tickets.urgency.${urgency}`) })}
    </span>
  );
}

/** The name of a topic ID (the generic word while its `topic.profile` is not read). */
export function topicLabel(t: (key: string) => string, topics: readonly Topic[], id: string): string {
  return topics.find((topic) => topic.id === id)?.name ?? t('tickets.topic.unknown');
}
