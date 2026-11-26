// The ticket filters of both task lists (E-41, ADR-100 sections 2 and 7): status, urgency, person and topic go to the
// hub (the list key, `core/tasks/filters.ts`); GitHub organisation and repository are sealed and filtered in the
// browser over the loaded rows with the bounded scan of the title search (`scan.ts`).
import { useId, useMemo } from 'react';
import { filterKey, NONE, type Order } from '../../../core/tasks/filters';
import { STATUSES, URGENCIES } from '../../../core/tasks/tickets';
import { useI18n } from '../../i18n';
import { usePeopleOf, useTopicsOf } from './tickets';

/** The chosen ticket filters ("" = any). */
export interface TicketFilterValues {
  readonly status: string;
  readonly urgency: string;
  /** A user ID, `none` or "". */
  readonly assignee: string;
  /** A topic ID, `none` or "". */
  readonly topic: string;
  readonly githubOrg: string;
  readonly githubRepo: string;
}

export const NO_FILTERS: TicketFilterValues = {
  status: '',
  urgency: '',
  assignee: '',
  topic: '',
  githubOrg: '',
  githubRepo: '',
};

/** Whether any ticket filter is set. */
export const ticketFiltered = (values: TicketFilterValues): boolean =>
  Object.values(values).some((value) => value.trim() !== '');

/**
 * The list key of the hub filters with the state filter `state` and the order `order` (latest activity, the default,
 * stays out of the key: the list is the same one the other screens load).
 */
export const hubKey = (state: string | null, values: TicketFilterValues, order: Order = 'activity'): string | null =>
  filterKey({
    state,
    status: values.status || null,
    urgency: values.urgency || null,
    assignee: values.assignee || null,
    topic: values.topic || null,
    order: order === 'activity' ? null : order,
  });

const SELECT = 'min-h-11 min-w-0 max-w-full flex-1 rounded border border-gray-300 px-2 sm:flex-none';
const FIELD = 'flex w-full min-w-0 items-center gap-2 sm:w-auto';

export function TicketFilters({
  org,
  orgs,
  values,
  onChange,
}: {
  readonly org: string;
  /** The organizations whose people and topics are offered (the task dashboard, ADR-101; default `org` alone). */
  readonly orgs?: readonly string[];
  readonly values: TicketFilterValues;
  readonly onChange: (values: TicketFilterValues) => void;
}) {
  const { t } = useI18n();
  const id = useId();
  const orgList = useMemo(() => orgs ?? [org], [orgs, org]);
  const people = usePeopleOf(orgList);
  const topicsBy = useTopicsOf(orgList);
  const topics = useMemo(() => [...topicsBy.values()].flat(), [topicsBy]);
  const set = (patch: Partial<TicketFilterValues>) => onChange({ ...values, ...patch });
  const select = (
    name: 'status' | 'urgency' | 'assignee' | 'topic',
    label: string,
    options: readonly { readonly value: string; readonly label: string }[],
  ) => (
    <div className={FIELD}>
      <label htmlFor={`${id}-${name}`} className="text-sm">
        {label}
      </label>
      <select
        id={`${id}-${name}`}
        className={SELECT}
        value={values[name]}
        onChange={(event) => set({ [name]: event.target.value } as Partial<TicketFilterValues>)}
        data-testid={`tickets.filter.${name}`}
      >
        <option value="">{t('tickets.filter.any')}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  );
  const text = (name: 'githubOrg' | 'githubRepo', label: string, placeholder: string, testId: string) => (
    <div className="flex w-full min-w-0 flex-col gap-1 sm:w-auto">
      <label htmlFor={`${id}-${name}`} className="text-sm">
        {label}
      </label>
      <input
        id={`${id}-${name}`}
        type="search"
        className="min-h-11 rounded border border-gray-300 px-2"
        value={values[name]}
        placeholder={placeholder}
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        onChange={(event) => set({ [name]: event.target.value } as Partial<TicketFilterValues>)}
        data-testid={testId}
      />
    </div>
  );
  return (
    <div className="flex flex-wrap items-end gap-x-4 gap-y-2" data-testid="tickets.filters">
      {select(
        'status',
        t('tickets.field.status'),
        STATUSES.map((value) => ({ value, label: t(`tickets.status.${value}`) })),
      )}
      {select(
        'urgency',
        t('tickets.field.urgency'),
        URGENCIES.map((value) => ({ value, label: t(`tickets.urgency.${value}`) })),
      )}
      {select('assignee', t('tickets.field.person'), [
        { value: NONE, label: t('tickets.filter.none-person') },
        ...people.map((person) => ({ value: person.id, label: person.name })),
      ])}
      {select('topic', t('tickets.field.topic'), [
        { value: NONE, label: t('tickets.filter.none-topic') },
        ...topics.map((topic) => ({ value: topic.id, label: topic.name })),
      ])}
      {text(
        'githubOrg',
        t('tickets.field.github-org'),
        t('tickets.filter.github-org.placeholder'),
        'tickets.filter.github-org',
      )}
      {text(
        'githubRepo',
        t('tickets.field.github-repo'),
        t('tickets.filter.github-repo.placeholder'),
        'tickets.filter.github-repo',
      )}
    </div>
  );
}
