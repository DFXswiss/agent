// The passkeys of `GET /v1/me` (06-web.md section 3.6): created, last used, whether the passkey unlocks the keys (PRF
// seen), the one that signed in this session; "Remove" (`DELETE /v1/passkeys/{id}`) behind a confirmation, not offered
// for the last passkey (the hub refuses it with `last_passkey`, e.g. when another device removed one meanwhile).
// Removing the passkey of this session ends the session (the hub ends it with the passkey): the browser signs out.
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { type PasskeyRow, passkeysOf, removePasskey } from '../../../../core/account/passkeys';
import { formatEventTime } from '../../../../core/i18n';
import { useStore } from '../../../hooks/useStore';
import { useNow } from '../../../countdown/useNow';
import { useI18n } from '../../../i18n';
import { Dialog } from '../../../screens/members/Dialog';
import { useAppEnv } from '../../../shell/context';
import { errorCode } from '../../../shell/errors';

export function PasskeyList() {
  const { app } = useAppEnv();
  const { t, error, locale } = useI18n();
  const navigate = useNavigate();
  const profile = useStore((state) => state.me.profile, app.store);
  const passkeys = passkeysOf(profile);
  const [removing, setRemoving] = useState<PasskeyRow | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const now = useNow(false);
  const when = (ms: number) =>
    formatEventTime(ms, { locale, timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone, now });

  const remove = async (row: PasskeyRow) => {
    setBusy(true);
    try {
      await removePasskey((method, target) => app.client.request(method, target), row.credentialId);
    } catch (caught) {
      setNotice(error(errorCode(caught)).title);
      setBusy(false);
      setRemoving(null);
      await app.loadMe().catch(() => null);
      return;
    }
    if (row.credentialId === app.credentialId) {
      await app.signOut();
      navigate('/login', { replace: true });
      return;
    }
    setBusy(false);
    setRemoving(null);
    setNotice(t('passkeys.removed'));
    await app.loadMe().catch(() => null);
  };

  return (
    <div className="flex flex-col gap-3">
      <ul className="flex flex-col gap-3" data-testid="passkeys.list">
        {passkeys.map((row, index) => (
          <li
            key={row.credentialId}
            className="flex flex-wrap items-center justify-between gap-3 rounded border border-gray-300 p-3"
            data-testid="passkeys.item"
            data-credential={row.credentialId}
            data-prf={row.prfSeen}
          >
            <div className="flex flex-col gap-1">
              <span className="font-semibold">
                {t('passkeys.item.name', { n: passkeys.length - index })}
                {row.credentialId === app.credentialId && (
                  <span className="ml-2 text-sm font-normal text-gray-700" data-testid="passkeys.item.current">
                    {t('passkeys.item.current')}
                  </span>
                )}
              </span>
              <span className="text-sm text-gray-700">{t('passkeys.item.created', { time: when(row.createdAt) })}</span>
              <span className="text-sm text-gray-700">
                {row.lastUsedAt === null
                  ? t('passkeys.item.never-used')
                  : t('passkeys.item.last-used', { time: when(row.lastUsedAt) })}
              </span>
              <span className="text-sm" data-testid="passkeys.item.prf">
                {row.prfSeen ? t('passkeys.item.prf') : t('passkeys.item.no-prf')}
              </span>
            </div>
            {passkeys.length > 1 ? (
              <button
                type="button"
                className="min-h-11 rounded border border-gray-400 px-4"
                onClick={() => {
                  setNotice(null);
                  setRemoving(row);
                }}
                data-testid="passkeys.remove"
              >
                {t('passkeys.remove')}
              </button>
            ) : (
              <span className="text-sm text-gray-700" data-testid="passkeys.last">
                {t('passkeys.last')}
              </span>
            )}
          </li>
        ))}
      </ul>
      {notice !== null && (
        <p role="status" data-testid="passkeys.notice">
          {notice}
        </p>
      )}
      {removing !== null && (
        <Dialog
          title={t('passkeys.remove.title')}
          testId="passkeys.remove-dialog"
          busy={busy}
          onClose={() => setRemoving(null)}
        >
          <p>
            {removing.credentialId === app.credentialId ? t('passkeys.remove.body-current') : t('passkeys.remove.body')}
          </p>
          <div className="flex flex-wrap gap-3">
            <button
              type="button"
              className="min-h-11 rounded bg-red-800 px-4 text-white disabled:opacity-60"
              onClick={() => void remove(removing)}
              disabled={busy}
              data-testid="passkeys.remove.confirm"
            >
              {t('passkeys.remove.confirm')}
            </button>
            <button
              type="button"
              className="min-h-11 rounded border border-gray-400 px-4"
              onClick={() => setRemoving(null)}
              disabled={busy}
              data-testid="passkeys.remove.cancel"
            >
              {t('passkeys.remove.cancel')}
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}
