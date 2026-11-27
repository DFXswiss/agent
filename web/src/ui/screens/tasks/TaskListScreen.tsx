// Task list of a workgroup (06-web.md section 5.1, route `/o/:org/w/:wg`, right `wg:read`): newest activity first
// with the decrypted title, state, owner and last activity, and the ticket fields of ADR-102 (status, urgency, person,
// topic); a state filter and the ticket filters (`TicketFilters.tsx`); a windowed list with "Load more"; the composer at
// the top for users with `wg:write`, with "Capture task" beside it (a task without an agent, E-41), who also get
// "Optimize processes" (an analysis of this workgroup, ADR-097 section 6); a
// link to the workgroup settings (WEB-09). The list is the shared task table (`TaskTable.tsx`, also used by the task
// overview of the organization). A workgroup the user cannot read is "not
// found" (existence is not revealed, 03-api.md section 1).
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { WG } from '../../../core/log/state';
import { useI18n } from '../../i18n';
import { Composer } from '../../composer/Composer';
import { AnalysisButton } from '../../analysis/AnalysisButton';
import { writableWorkgroups } from '../../../core/tasks/send';
import { useDirectory, useOrgView } from './hooks';
import { StateFilter, TaskTable } from './TaskTable';
import { CaptureForm } from './CaptureForm';
import { hubKey, NO_FILTERS, type TicketFilterValues, TicketFilters, ticketFiltered } from './TicketFilters';
import { Unlocking } from '../../unlock/Unlocking';

export interface TaskListProps {
  readonly org: string;
  readonly wg: string;
}

/** The rows of a workgroup's list with the state and ticket filters and paging. */
export function TaskList({ org, wg }: TaskListProps) {
  const { t } = useI18n();
  const [filter, setFilter] = useState<string | null>(null);
  const [tickets, setTickets] = useState<TicketFilterValues>(NO_FILTERS);
  const filtered = filter !== null || ticketFiltered(tickets);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
        <StateFilter value={filter} onChange={setFilter} />
        <TicketFilters org={org} values={tickets} onChange={setTickets} />
      </div>
      <TaskTable
        org={org}
        wgs={[wg]}
        state={hubKey(filter, tickets)}
        githubOrg={tickets.githubOrg}
        githubRepo={tickets.githubRepo}
        empty={
          filtered ? (
            <div className="flex flex-wrap items-center gap-2 text-gray-700">
              <p>{t('tasks.overview.no-match')}</p>
              <button
                type="button"
                className="min-h-11 underline"
                onClick={() => {
                  setFilter(null);
                  setTickets(NO_FILTERS);
                }}
                data-testid="tasks.filter.reset"
              >
                {t('tasks.filter.reset')}
              </button>
            </div>
          ) : (
            <p className="text-gray-600">{t('tasks.empty')}</p>
          )
        }
      />
    </div>
  );
}

export function TaskListScreen() {
  const { org, wg } = useParams() as { readonly org: string; readonly wg: string };
  const { t } = useI18n();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  if (state === null) {
    return (
      <p className="p-4" data-testid="tasks.loading">
        {t('tasks.loading')}
      </p>
    );
  }
  if (userId === null || !state.canRead(userId, WG, wg)) {
    return (
      <p className="p-4" data-testid="tasks.not-found">
        {t('tasks.not-found')}
      </p>
    );
  }
  const wgName = (id: string) => directory.workgroups.get(id) ?? t('tasks.wg.unnamed');
  const name = wgName(wg);
  const writer = state.canWriteWg(userId, wg);
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="tasks.screen">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold" data-testid="tasks.title">
          {name}
        </h1>
        <div className="flex flex-wrap items-center gap-2">
          {writer && (
            <AnalysisButton
              org={org}
              scope="wg"
              workgroups={writableWorkgroups(state, userId)}
              initial={wg}
              name={wgName}
            />
          )}
          <Link
            className="flex min-h-11 items-center text-sm text-blue-800 underline"
            to={`/o/${encodeURIComponent(org)}/w/${encodeURIComponent(wg)}/settings`}
            data-testid="tasks.settings"
          >
            {t('wg-settings.link')}
          </Link>
        </div>
      </div>
      <Unlocking org={org} wg={wg} />
      {writer && <Composer org={org} wg={wg} />}
      {writer && <CaptureForm org={org} wgs={[wg]} name={wgName} />}
      <TaskList org={org} wg={wg} />
    </main>
  );
}
