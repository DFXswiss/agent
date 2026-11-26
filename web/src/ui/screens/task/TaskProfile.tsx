// The rights profile of a task (ADR-094 sections 1 and 7): "Every right" without one (the default, E-11), else the
// profile's name; writers of the workgroup choose a profile of the workgroup or none, saved with
// `PUT /v1/tasks/{task}/agent-profile` (it applies at once to the task's sessions and their subagents; `409
// peer_outdated` when a device of the organization runs an older runner). The profile of an analysis task is never
// widened (ADR-097 section 2): it shows no change button but the reason and a link to start an ordinary task.
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { isAnalysis } from '../../../core/analysis/analysis';
import {
  listProfiles,
  profileLabel,
  profileOf,
  setTaskProfile,
  type ListedProfile,
} from '../../../core/profiles/profiles';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { sendErrorCode } from '../../../core/tasks/send';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import '../../../core/i18n/lazy/analysis';

const BUTTON = 'min-h-11 rounded border border-gray-300 px-3 text-sm';
const NONE = '';

export function TaskProfile({ row, writer }: { readonly row: TaskRow; readonly writer: boolean }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const current = profileOf(row.agent_profile);
  const analysis = isAnalysis(row);
  const [choices, setChoices] = useState<readonly ListedProfile[] | null>(null);
  const [value, setValue] = useState(current?.name ?? NONE);
  const [failed, setFailed] = useState<string | null>(null);
  const edit = () => {
    setFailed(null);
    setValue(current?.name ?? NONE);
    listProfiles(app.tasks, row.wg_id).then(
      (found) => setChoices(found.profiles),
      (caught: unknown) => setFailed(sendErrorCode(caught)),
    );
  };
  const save = () => {
    setTaskProfile(app.tasks, row.task_id, value === NONE ? null : value).then(
      () => setChoices(null),
      (caught: unknown) => setFailed(sendErrorCode(caught)),
    );
  };
  return (
    <div className="flex flex-col gap-2 text-sm" data-testid="task.profile">
      <p className="flex flex-wrap items-center gap-2">
        <span>{t('profiles.task.current')}</span>
        <span data-testid="task.profile.name" data-profile={current?.name ?? ''}>
          {profileLabel(current, t('profiles.task.none'))}
        </span>
        {writer && !analysis && choices === null && (
          <button type="button" className={BUTTON} onClick={edit} data-testid="task.profile.change">
            {t('profiles.task.change')}
          </button>
        )}
      </p>
      {analysis && (
        <p className="flex flex-wrap items-center gap-2 text-gray-700" data-testid="task.profile.analysis">
          <span>{t('analysis.profile.locked')}</span>
          {writer && (
            <Link
              className="flex min-h-11 items-center text-blue-800 underline"
              to={`/o/${encodeURIComponent(row.org_id)}/w/${encodeURIComponent(row.wg_id)}`}
              data-testid="task.profile.new"
            >
              {t('analysis.profile.new')}
            </Link>
          )}
        </p>
      )}
      {choices !== null && (
        <div className="flex flex-wrap items-center gap-2">
          <select
            className="min-h-11 rounded border border-gray-300 px-2"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            data-testid="task.profile.select"
          >
            <option value={NONE}>{t('profiles.task.none')}</option>
            {choices.map((item) => (
              <option key={item.name} value={item.name}>
                {item.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="min-h-11 rounded bg-blue-700 px-3 text-white"
            onClick={save}
            data-testid="task.profile.save"
          >
            {t('profiles.save')}
          </button>
          <button type="button" className={BUTTON} onClick={() => setChoices(null)} data-testid="task.profile.cancel">
            {t('profiles.cancel')}
          </button>
        </div>
      )}
      {failed !== null && (
        <p role="alert" className="text-red-800" data-testid="task.profile.error">
          {error(failed).title}
        </p>
      )}
    </div>
  );
}
