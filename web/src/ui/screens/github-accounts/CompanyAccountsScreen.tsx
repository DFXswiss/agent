// Company GitHub accounts of an organization (ADR-078 sections 1 and 3, key model KM-35, route `/o/:org/github`): the
// organization's accounts by metadata – "Company account of <org> · connected <date>", the login only where this
// browser stored the record – since no browser can open them (they are sealed to the organization's devices), and
// which one is the organization's default. Admins connect a company account (`github.connect` with `org_id` to a
// device of the organization; the final answer's body is sealed and stored by `storeCompanyIdentity`), reconnect one
// (same record, version + 1), remove one (confirmation, `DELETE /v1/github-identities/{id}`) and set or clear the
// organization's default (`PUT /v1/github-defaults/org/{org}`; `409 peer_outdated` names the devices to update).
// Members see the list without controls. An organization whose log is not verified yet shows "being checked".
import { useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  type CompanyIdentity,
  companyBody,
  removeIdentity,
  setGithubDefault,
  storeCompanyIdentity,
} from '../../../core/github/identities';
import { RequestError, type RequestView } from '../../../core/requests/client';
import { useI18n } from '../../i18n';
import { Dialog } from '../members/Dialog';
import { useAppEnv } from '../../shell/context';
import { ConnectPanel } from '../../settings/sections/github/ConnectPanel';
import { useIdentities, useIdentityText } from '../../settings/sections/github/identities';
import { useReason } from '../../settings/sections/github/reason';
import { useDirectory, useOrgView } from '../tasks/hooks';
import { ChangeFailed } from './Outdated';

const BUTTON = 'min-h-11 rounded border border-gray-400 px-3 text-sm';

type Open =
  | { readonly kind: 'add' }
  | { readonly kind: 'reconnect'; readonly id: string }
  | { readonly kind: 'remove'; readonly identity: CompanyIdentity }
  | null;

export function CompanyAccountsScreen() {
  const { org } = useParams() as { readonly org: string };
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const { state: log, userId } = useOrgView(org);
  const directory = useDirectory(org, log !== null);
  const { state, error, reload } = useIdentities();
  const text = useIdentityText(state.knownLogins, directory.orgName);
  const [open, setOpen] = useState<Open>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<unknown>(null);
  if (log === null)
    return (
      <p className="p-4" data-testid="company-github.unverified">
        {t('identities.unverified')}
      </p>
    );
  const admin = userId !== null && log.isAdmin(userId);
  const accounts = state.identities.filter(
    (item): item is CompanyIdentity => item.status === 'company' && item.ownerId === org,
  );
  const orgDefault = state.defaults.find((item) => item.scope === 'org' && item.scopeId === org)?.identityId ?? null;
  const done = () => {
    setOpen(null);
    reload();
  };
  const act = async (action: () => Promise<void>) => {
    setBusy(true);
    setFailed(null);
    try {
      await action();
      done();
    } catch (caught) {
      setFailed(caught);
    } finally {
      setBusy(false);
    }
  };
  const complete = (reconnect: string | null) => async (view: RequestView) => {
    const body = companyBody(view);
    if (body === null) throw new RequestError('params_invalid');
    await storeCompanyIdentity(app.tasks, org, body, reconnect);
  };
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="company-github.screen">
      <h1 className="text-xl font-semibold">
        {t('identities.company.title')}
        {directory.orgName !== null && <span className="block text-base font-normal">{directory.orgName}</span>}
      </h1>
      <p className="text-sm text-gray-700">{t('identities.company.intro')}</p>
      {error !== null && (
        <div className="flex flex-col gap-2">
          <p role="alert" data-testid="company-github.load-failed">
            {t('identities.load-failed', { reason: reason(error) })}
          </p>
          <button type="button" className={`${BUTTON} self-start`} onClick={reload} data-testid="company-github.reload">
            {t('github.retry')}
          </button>
        </div>
      )}
      {state.status === 'unknown' && error === null && (
        <p data-testid="company-github.loading">{t('identities.loading')}</p>
      )}
      {state.status === 'loaded' && accounts.length === 0 && (
        <p data-testid="company-github.empty">{t('identities.company.none')}</p>
      )}
      {accounts.length > 0 && (
        <ul className="flex flex-col gap-2" data-testid="company-github.list">
          {accounts.map((identity) => {
            const isDefault = identity.id === orgDefault;
            return (
              <li
                key={identity.id}
                className="flex flex-col gap-2 rounded border border-gray-200 p-3"
                data-testid="company-github.item"
                data-identity={identity.id}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="break-all font-medium" data-testid="company-github.label">
                    {text(identity)}
                  </span>
                  {isDefault && (
                    <span
                      className="rounded bg-blue-100 px-2 text-xs text-blue-900"
                      data-testid="company-github.default"
                    >
                      {t('identities.org-default')}
                    </span>
                  )}
                </div>
                {admin && (
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      className={BUTTON}
                      disabled={busy}
                      onClick={() =>
                        void act(() => setGithubDefault(app.tasks, 'org', org, isDefault ? null : identity.id))
                      }
                      data-testid={isDefault ? 'company-github.clear-default' : 'company-github.make-default'}
                    >
                      {t(isDefault ? 'identities.default.clear' : 'identities.default.set')}
                    </button>
                    <button
                      type="button"
                      className={BUTTON}
                      onClick={() => setOpen({ kind: 'reconnect', id: identity.id })}
                      data-testid="company-github.reconnect"
                    >
                      {t('identities.reconnect')}
                    </button>
                    <button
                      type="button"
                      className={BUTTON}
                      onClick={() => setOpen({ kind: 'remove', identity })}
                      data-testid="company-github.remove"
                    >
                      {t('identities.remove')}
                    </button>
                  </div>
                )}
                {open?.kind === 'reconnect' && open.id === identity.id && (
                  <ConnectPanel
                    title={t('identities.reconnect.title', { label: text(identity) })}
                    options={{ orgId: org }}
                    complete={complete(identity.id)}
                    onConnected={done}
                    onClose={() => setOpen(null)}
                    testId="company-github.reconnect-panel"
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}
      {failed !== null && open?.kind !== 'remove' && (
        <ChangeFailed org={org} error={failed} testId="company-github.failed" />
      )}
      {admin &&
        (open?.kind === 'add' ? (
          <ConnectPanel
            title={t('identities.company.add')}
            options={{ orgId: org }}
            complete={complete(null)}
            onConnected={done}
            onClose={() => setOpen(null)}
            testId="company-github.add-panel"
          />
        ) : (
          <button
            type="button"
            className={`${BUTTON} self-start`}
            onClick={() => setOpen({ kind: 'add' })}
            data-testid="company-github.add"
          >
            {t('identities.company.add')}
          </button>
        ))}
      {open?.kind === 'remove' && (
        <Dialog
          title={t('identities.remove.title', { label: text(open.identity) })}
          testId="company-github.remove.dialog"
          busy={busy}
          onClose={() => setOpen(null)}
        >
          <p>{t('identities.company.remove.body')}</p>
          {failed !== null && <ChangeFailed org={org} error={failed} testId="company-github.remove.failed" />}
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="min-h-11 rounded bg-red-700 px-4 text-white"
              disabled={busy}
              onClick={() => void act(() => removeIdentity(app.tasks, open.identity.id))}
              data-testid="company-github.remove.confirm"
            >
              {t('identities.remove')}
            </button>
            <button
              type="button"
              className={BUTTON}
              disabled={busy}
              onClick={() => setOpen(null)}
              data-testid="company-github.remove.cancel"
            >
              {t('github.cancel')}
            </button>
          </div>
        </Dialog>
      )}
    </main>
  );
}
