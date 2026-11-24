// The default GitHub identity of a workgroup (ADR-076 section 3, `PUT /v1/github-defaults/wg/{wg}`): only company
// accounts of the workgroup's organization, chosen by its admins and the writers of the workgroup; "none" lets the
// organization's default apply. Readers see the current choice. A `409 peer_outdated` names the devices to update.
import { useId, useState } from 'react';
import { type CompanyIdentity, loadIdentities, setGithubDefault } from '../../../core/github/identities';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useIdentities, useIdentityText } from '../../settings/sections/github/identities';
import { ChangeFailed } from '../github-accounts/Outdated';

const NONE = '';

export function GithubDefault({
  org,
  wg,
  orgName,
  editable,
}: {
  readonly org: string;
  readonly wg: string;
  readonly orgName: string | null;
  readonly editable: boolean;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const id = useId();
  const { state } = useIdentities();
  const text = useIdentityText(state.knownLogins, orgName);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<unknown>(null);
  const accounts = state.identities.filter(
    (item): item is CompanyIdentity => item.status === 'company' && item.ownerId === org,
  );
  const current = state.defaults.find((item) => item.scope === 'wg' && item.scopeId === wg)?.identityId ?? NONE;
  const change = async (value: string) => {
    setBusy(true);
    setFailed(null);
    try {
      await setGithubDefault(app.tasks, 'wg', wg, value === NONE ? null : value);
      await loadIdentities(app.tasks);
    } catch (caught) {
      setFailed(caught);
    } finally {
      setBusy(false);
    }
  };
  if (state.status === 'unknown') return null;
  const chosen = accounts.find((item) => item.id === current);
  const shown = chosen === undefined ? t('identities.wg-default.none') : text(chosen);
  return (
    <section className="flex flex-col gap-2" data-testid="wg-settings.github" aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="font-medium">
        {t('identities.wg-default.title')}
      </h2>
      <p className="text-sm text-gray-700">{t('identities.wg-default.intro')}</p>
      {editable ? (
        <>
          <label htmlFor={id} className="sr-only">
            {t('identities.wg-default.title')}
          </label>
          <select
            id={id}
            className="min-h-11 rounded border border-gray-400 px-3"
            value={current}
            disabled={busy}
            onChange={(event) => void change(event.target.value)}
            data-testid="wg-settings.github.select"
          >
            <option value={NONE}>{t('identities.wg-default.none')}</option>
            {accounts.map((item) => (
              <option key={item.id} value={item.id}>
                {text(item)}
              </option>
            ))}
          </select>
        </>
      ) : (
        <p data-testid="wg-settings.github.view">{shown}</p>
      )}
      {failed !== null && <ChangeFailed org={org} error={failed} testId="wg-settings.github.failed" />}
    </section>
  );
}
