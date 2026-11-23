// The non-blocking "Sign in again" prompt (06-web.md section 2.6): shown when less than five minutes of the session
// remain or the hub answered `session_expired`. Signing in again replaces the session key without leaving the screen;
// the identity in memory stays. A passkey of another account ends this one and goes to the sign-in page.
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CeremonyCancelled } from '../../core/account/ceremony';
import { signIn } from '../../core/account/login';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from './context';
import { errorCode } from './errors';

type Status = 'idle' | 'working' | 'cancelled' | 'other-user' | { readonly code: string };

/** Signing in again with a passkey (also used by the session-expired page); `onRenewed` runs after success. */
export function useRenewal(onRenewed: () => void = () => undefined) {
  const { app, credentials, origin } = useAppEnv();
  const { t, error } = useI18n();
  const navigate = useNavigate();
  const [status, setStatus] = useState<Status>('idle');
  const renew = async () => {
    setStatus('working');
    try {
      const result = await signIn({ anon: app.anon, credentials, origin });
      if (result.userId !== app.userId) {
        await app.signOut();
        setStatus('other-user');
        navigate('/login', { replace: true });
        return;
      }
      await app.begin(result);
      setStatus('idle');
      onRenewed();
    } catch (caught) {
      setStatus(caught instanceof CeremonyCancelled ? 'cancelled' : { code: errorCode(caught) });
    }
  };
  const message =
    typeof status === 'object'
      ? error(status.code).title
      : status === 'cancelled' || status === 'other-user'
        ? t(`shell.renew.${status}`)
        : null;
  return { working: status === 'working', renew, message };
}

export function RenewalNotice() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const notice = useStore((state) => state.connection.session, app.store);
  const { working, renew, message } = useRenewal();
  if (notice === 'ok') return null;
  return (
    <div
      className="flex flex-wrap items-center gap-3 bg-amber-50 px-4 py-2 text-amber-900"
      role="status"
      data-testid="shell.renew"
    >
      <span data-testid="shell.renew.text">{t(`shell.renew.${notice}`)}</span>
      <button
        type="button"
        className="min-h-11 rounded border border-amber-700 px-3"
        onClick={() => void renew()}
        disabled={working}
        data-testid="shell.renew.submit"
      >
        {working ? t('shell.renew.working') : t('shell.renew.action')}
      </button>
      {message !== null && <span data-testid="shell.renew.message">{message}</span>}
    </div>
  );
}
