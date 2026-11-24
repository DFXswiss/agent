// The GitHub identity of a task (ADR-076 section 3): its chosen identity ("Automatic" without a choice, "another
// account" when the browser cannot name it), and for writers of the workgroup "Change" – the identities the task may
// use or "Automatic" – saved with `PUT /v1/tasks/{task}/github-identity` (`409 peer_outdated` names the devices to
// update, `403 identity_forbidden` is refused by the hub).
import { useState } from 'react';
import { setTaskIdentity } from '../../../core/github/identities';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { AUTOMATIC, IdentityChoice } from '../../composer/IdentityChoice';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { IdentityName, useIdentities } from '../../settings/sections/github/identities';
import { ChangeFailed } from '../github-accounts/Outdated';

const BUTTON = 'min-h-11 rounded border border-gray-300 px-3 text-sm';

export function TaskIdentity({
  row,
  writer,
  orgName,
}: {
  readonly row: TaskRow;
  readonly writer: boolean;
  readonly orgName: string | null;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  useIdentities();
  const chosen = row.github_identity ?? null;
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(chosen ?? AUTOMATIC);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<unknown>(null);
  const save = async () => {
    setBusy(true);
    setFailed(null);
    try {
      await setTaskIdentity(app.tasks, row.task_id, value === AUTOMATIC ? null : value);
      setEditing(false);
    } catch (caught) {
      setFailed(caught);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-2 text-sm" data-testid="task.identity">
      <p className="flex flex-wrap items-center gap-2">
        <span>{t('identities.task.current')}</span>
        {chosen === null ? (
          <span data-testid="task.identity.automatic">{t('identities.task.automatic')}</span>
        ) : (
          <IdentityName id={chosen} orgName={orgName} />
        )}
        {writer && !editing && (
          <button
            type="button"
            className={BUTTON}
            onClick={() => {
              setValue(chosen ?? AUTOMATIC);
              setEditing(true);
            }}
            data-testid="task.identity.change"
          >
            {t('identities.task.change')}
          </button>
        )}
      </p>
      {editing && (
        <div className="flex flex-wrap items-end gap-2">
          <IdentityChoice
            org={row.org_id}
            orgName={orgName}
            value={value}
            onChange={setValue}
            disabled={busy}
            testId="task.identity.select"
          />
          <button
            type="button"
            className="min-h-11 rounded bg-blue-700 px-3 text-white"
            disabled={busy}
            onClick={() => void save()}
            data-testid="task.identity.save"
          >
            {t('identities.task.save')}
          </button>
          <button
            type="button"
            className={BUTTON}
            disabled={busy}
            onClick={() => setEditing(false)}
            data-testid="task.identity.cancel"
          >
            {t('github.cancel')}
          </button>
        </div>
      )}
      {failed !== null && <ChangeFailed org={row.org_id} error={failed} testId="task.identity.failed" />}
    </div>
  );
}
