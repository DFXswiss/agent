// The user's own GitHub accounts in the settings section "GitHub" (KOR-47, ADR-078, 06-web.md section 8.1): every own
// identity verified and opened in the browser (login and display name, primary badge, origin, scopes, signing key,
// connection date – never the token), with "Make primary" (`PUT /v1/github-identities/{id}/primary`), "Reconnect"
// (`github.connect` with `identity_id`: same record, version + 1) and, for further accounts, "Disconnect" (confirmation,
// `DELETE /v1/github-identities/{id}`); the primary account is disconnected with the section's own flow above.
// "Add another account" connects a further account (`github.connect` without `identity_id`, optionally with a signing
// key). After every change the identities and the primary connection are loaded again. Texts from GitHub are rendered
// as text only.
import { useState } from 'react';
import { loadGithub } from '../../../../core/github/connection';
import { type GithubIdentity, makePrimary, removeIdentity } from '../../../../core/github/identities';
import { requestErrorCode } from '../../../../core/requests/client';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { Dialog } from '../../../screens/members/Dialog';
import { useAppEnv } from '../../../shell/context';
import { ConnectPanel } from './ConnectPanel';
import { useDate, useIdentities, useIdentityText } from './identities';
import { useReason } from './reason';

const BUTTON = 'min-h-11 rounded border border-gray-400 px-3 text-sm';

type Open =
  | { readonly kind: 'add' }
  | { readonly kind: 'reconnect'; readonly id: string }
  | { readonly kind: 'remove'; readonly identity: GithubIdentity }
  | null;

function Details({ identity }: { readonly identity: GithubIdentity }) {
  const { t } = useI18n();
  const date = useDate();
  if (identity.status !== 'verified') return null;
  const { account } = identity;
  return (
    <ul className="flex flex-col text-sm text-gray-700">
      <li data-testid="github.accounts.source" data-source={account.source}>
        {t('github.source', { source: t(`github.source.${account.source}`) })}
      </li>
      <li data-testid="github.accounts.scopes">
        {account.scopes.length > 0
          ? t('github.scopes', { scopes: account.scopes.join(', ') })
          : t(`github.scopes.none.${account.source}`)}
      </li>
      <li data-testid="github.accounts.signing" data-signing={account.signingKey}>
        {t(account.signingKey ? 'identities.signing.yes' : 'identities.signing.no')}
      </li>
      <li>{t('identities.connected-at', { date: date(account.connectedAt) })}</li>
    </ul>
  );
}

export function GithubAccounts({ primaryConnected }: { readonly primaryConnected: boolean }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const { state, error, reload } = useIdentities();
  const text = useIdentityText(state.knownLogins, null);
  const [open, setOpen] = useState<Open>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const userId = useStore((current) => current.me.userId, app.store);
  const own = state.identities.filter((item) => item.ownerKind === 'user' && item.ownerId === userId);
  const refresh = () => {
    setOpen(null);
    reload();
    loadGithub(app.tasks).catch(() => undefined);
  };
  const act = async (action: () => Promise<void>) => {
    setBusy(true);
    setFailed(null);
    try {
      await action();
      refresh();
    } catch (caught) {
      setFailed(requestErrorCode(caught));
    } finally {
      setBusy(false);
    }
  };
  if (error !== null)
    return (
      <div className="flex flex-col gap-2">
        <p role="alert" data-testid="github.accounts.load-failed">
          {t('identities.load-failed', { reason: reason(error) })}
        </p>
        <button type="button" className={`${BUTTON} self-start`} onClick={reload} data-testid="github.accounts.reload">
          {t('github.retry')}
        </button>
      </div>
    );
  if (state.status === 'unknown' || (own.length === 0 && !primaryConnected)) return null;
  return (
    <section className="flex flex-col gap-3" aria-labelledby="github-accounts-title" data-testid="github.accounts">
      <h3 id="github-accounts-title" className="font-semibold">
        {t('identities.own.title')}
      </h3>
      <ul className="flex flex-col gap-2" data-testid="github.accounts.list">
        {own.map((identity) => (
          <li
            key={identity.id}
            className="flex flex-col gap-2 rounded border border-gray-200 p-3"
            data-testid="github.accounts.item"
            data-identity={identity.id}
            data-status={identity.status}
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="break-all font-medium" data-testid="github.accounts.login">
                {text(identity)}
              </span>
              {identity.primary && (
                <span className="rounded bg-blue-100 px-2 text-xs text-blue-900" data-testid="github.accounts.primary">
                  {t('identities.primary')}
                </span>
              )}
            </div>
            <Details identity={identity} />
            <div className="flex flex-wrap gap-2">
              {!identity.primary && identity.status === 'verified' && (
                <button
                  type="button"
                  className={BUTTON}
                  disabled={busy}
                  onClick={() => void act(() => makePrimary(app.tasks, identity.id))}
                  data-testid="github.accounts.make-primary"
                >
                  {t('identities.make-primary')}
                </button>
              )}
              <button
                type="button"
                className={BUTTON}
                onClick={() => setOpen({ kind: 'reconnect', id: identity.id })}
                data-testid="github.accounts.reconnect"
              >
                {t('identities.reconnect')}
              </button>
              {!identity.primary && (
                <button
                  type="button"
                  className={BUTTON}
                  onClick={() => setOpen({ kind: 'remove', identity })}
                  data-testid="github.accounts.remove"
                >
                  {t('github.disconnect')}
                </button>
              )}
            </div>
            {open?.kind === 'reconnect' && open.id === identity.id && (
              <ConnectPanel
                title={t('identities.reconnect.title', { label: text(identity) })}
                options={{ identityId: identity.id }}
                onConnected={refresh}
                onClose={() => setOpen(null)}
                testId="github.accounts.reconnect-panel"
              />
            )}
          </li>
        ))}
      </ul>
      {failed !== null && open?.kind !== 'remove' && (
        <p role="alert" data-testid="github.accounts.failed">
          {t('identities.failed', { reason: reason(failed) })}
        </p>
      )}
      {open?.kind === 'add' ? (
        <ConnectPanel
          title={t('identities.add')}
          options={{}}
          onConnected={refresh}
          onClose={() => setOpen(null)}
          testId="github.accounts.add-panel"
        />
      ) : (
        <button
          type="button"
          className={`${BUTTON} self-start`}
          onClick={() => setOpen({ kind: 'add' })}
          data-testid="github.accounts.add"
        >
          {t('identities.add')}
        </button>
      )}
      {open?.kind === 'remove' && (
        <Dialog
          title={t('identities.remove.title', { label: text(open.identity) })}
          testId="github.accounts.remove.dialog"
          busy={busy}
          onClose={() => setOpen(null)}
        >
          <p>
            {t(
              open.identity.status === 'verified' && open.identity.account.revocable
                ? 'github.disconnect.body.revoked'
                : 'github.disconnect.body.kept',
            )}
          </p>
          {failed !== null && (
            <p role="alert" data-testid="github.accounts.remove.failed">
              {t('github.disconnect.failed', { reason: reason(failed) })}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="min-h-11 rounded bg-red-700 px-4 text-white"
              disabled={busy}
              onClick={() => void act(() => removeIdentity(app.tasks, open.identity.id))}
              data-testid="github.accounts.remove.confirm"
            >
              {t('github.disconnect')}
            </button>
            <button
              type="button"
              className={BUTTON}
              disabled={busy}
              onClick={() => setOpen(null)}
              data-testid="github.accounts.remove.cancel"
            >
              {t('github.cancel')}
            </button>
          </div>
        </Dialog>
      )}
    </section>
  );
}
