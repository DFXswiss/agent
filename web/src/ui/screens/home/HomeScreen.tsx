// Home of an organization (06-web.md section 4.1, route `/o/:org`, right `org:member`): right after the first login it
// proposes a first task. Context line with the organization and the workgroup selector (preselected with the default
// workgroup; only workgroups with `wg:write`), suggestion chips that fill the composer, the free prompt field, and the
// recent tasks of the default workgroup with a link to its full list. A user without write access anywhere sees the
// recent tasks only (controls the rights do not allow are not rendered, section 1).
import { useId, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { writableWorkgroups } from '../../../core/tasks/send';
import { useI18n } from '../../i18n';
import { Composer } from '../../composer/Composer';
import { useDirectory, useOrgView } from '../tasks/hooks';
import { TaskList } from '../tasks/TaskListScreen';

/** Recent tasks shown on the home screen. */
export const RECENT = 5;

export function HomeScreen() {
  const { org } = useParams() as { readonly org: string };
  const { t } = useI18n();
  const selectId = useId();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  const [chosen, setChosen] = useState<string | null>(null);
  if (state === null)
    return (
      <p className="p-4" data-testid="home.loading">
        {t('home.loading')}
      </p>
    );
  const writable = writableWorkgroups(state, userId);
  const wg = chosen !== null && writable.includes(chosen) ? chosen : writable[0];
  const wgName = (id: string) =>
    directory.workgroups.get(id) ?? (id === state.defaultWg ? t('home.wg.default') : t('home.wg.unnamed'));
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="home.screen">
      <h1 className="text-xl font-semibold">{t('home.title')}</h1>
      <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="home.context">
        <span data-testid="home.org">{directory.orgName ?? t('home.org.unnamed')}</span>
        <span aria-hidden="true">/</span>
        {wg !== undefined && (
          <>
            <label htmlFor={selectId} className="sr-only">
              {t('home.wg.label')}
            </label>
            <select
              id={selectId}
              className="min-h-11 rounded border border-gray-300 px-2"
              value={wg}
              onChange={(event) => setChosen(event.target.value)}
              data-testid="home.wg"
            >
              {writable.map((id) => (
                <option key={id} value={id}>
                  {wgName(id)}
                </option>
              ))}
            </select>
          </>
        )}
      </div>
      {wg === undefined ? (
        <p className="text-gray-700" data-testid="home.read-only">
          {t('home.read-only')}
        </p>
      ) : (
        <Composer org={org} wg={wg} suggestions />
      )}
      <section className="flex flex-col gap-2" data-testid="home.recent">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">{t('home.recent', { wg: wgName(state.defaultWg) })}</h2>
          <Link
            className="flex min-h-11 items-center underline"
            to={`/o/${encodeURIComponent(org)}/w/${encodeURIComponent(state.defaultWg)}`}
            data-testid="home.all-tasks"
          >
            {t('home.all-tasks')}
          </Link>
        </div>
        <TaskList org={org} wg={state.defaultWg} limit={RECENT} />
      </section>
    </main>
  );
}
