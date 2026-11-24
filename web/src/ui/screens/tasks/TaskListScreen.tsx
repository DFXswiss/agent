// Task list of a workgroup (06-web.md section 5.1, route `/o/:org/w/:wg`, right `wg:read`): newest activity first
// with the decrypted title, state, owner and last activity; a state filter; endless scrolling; the composer at the top
// for users with `wg:write`; a link to the workgroup settings (WEB-09). The list is the shared task table
// (`TaskTable.tsx`, also used by the task overview of the organization). A workgroup the user cannot read is "not
// found" (existence is not revealed, 03-api.md section 1).
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { WG } from '../../../core/log/state';
import { useI18n } from '../../i18n';
import { Composer } from '../../composer/Composer';
import { useDirectory, useOrgView } from './hooks';
import { StateFilter, TaskTable } from './TaskTable';

export interface TaskListProps {
  readonly org: string;
  readonly wg: string;
}

/** The rows of a workgroup's list with the state filter and paging. */
export function TaskList({ org, wg }: TaskListProps) {
  const { t } = useI18n();
  const [filter, setFilter] = useState<string | null>(null);
  return (
    <div className="flex flex-col gap-2">
      <StateFilter value={filter} onChange={setFilter} />
      <TaskTable org={org} wgs={[wg]} state={filter} empty={<p className="text-gray-600">{t('tasks.empty')}</p>} />
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
  const name = directory.workgroups.get(wg) ?? t('tasks.wg.unnamed');
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="tasks.screen">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold" data-testid="tasks.title">
          {name}
        </h1>
        <Link
          className="flex min-h-11 items-center text-sm text-blue-800 underline"
          to={`/o/${encodeURIComponent(org)}/w/${encodeURIComponent(wg)}/settings`}
          data-testid="tasks.settings"
        >
          {t('wg-settings.link')}
        </Link>
      </div>
      {state.canWriteWg(userId, wg) && <Composer org={org} wg={wg} />}
      <TaskList org={org} wg={wg} />
    </main>
  );
}
