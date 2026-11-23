// The recovery code in `/settings` (06-web.md section 3.2): a skipped confirmation leaves the "Recovery code not
// confirmed" reminder until the user types the code here; the code is checked against the recovery slot (signed
// `GET /v1/me/bundles/recovery`, opened in the browser and compared with the identity in memory; nothing is rotated),
// then the reminder is cleared. A new code replaces the old one after every recovery (`/recover`) and whenever the user
// asks for one (`NewRecoveryCode`, KOR-01).
import { type FormEvent, useState } from 'react';
import { checkRecoveryCode } from '../../../../core/account/passkeys';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { errorCode } from '../../../shell/errors';

export function RecoveryCode() {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const unconfirmed = useStore((state) => state.session.recoveryUnconfirmed, app.store);
  const identity = useStore((state) => state.me.identity, app.store);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ readonly ok: boolean; readonly text: string } | null>(null);

  const check = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    try {
      await checkRecoveryCode(app.userId!, code, await app.recoveryBundle(), identity!);
      app.setRecoveryUnconfirmed(false);
      setCode('');
      setNotice({ ok: true, text: t('passkeys.recovery.confirmed') });
    } catch (caught) {
      const failed = errorCode(caught);
      setNotice({ ok: false, text: failed === 'decrypt_failed' ? t('passkeys.recovery.wrong') : error(failed).title });
    }
    setBusy(false);
  };

  return (
    <form className="flex flex-col gap-3" onSubmit={(event) => void check(event)} data-testid="passkeys.recovery">
      <h3 className="text-lg font-semibold">{t('passkeys.recovery.title')}</h3>
      {unconfirmed && (
        <p className="bg-red-50 px-3 py-2 text-red-900" data-testid="passkeys.recovery.unconfirmed">
          {t('passkeys.recovery.unconfirmed')}
        </p>
      )}
      <p>{t('passkeys.recovery.explain')}</p>
      <label htmlFor="passkeys-recovery-code">{t('passkeys.recovery.label')}</label>
      <input
        id="passkeys-recovery-code"
        className="min-h-11 rounded border border-gray-400 px-3 font-mono uppercase"
        autoComplete="off"
        spellCheck={false}
        value={code}
        onChange={(event) => setCode(event.target.value)}
        data-testid="passkeys.recovery.input"
      />
      <button
        type="submit"
        className="min-h-11 self-start rounded border border-gray-400 px-4 disabled:opacity-60"
        disabled={busy}
        data-testid="passkeys.recovery.submit"
      >
        {t('passkeys.recovery.submit')}
      </button>
      {notice !== null && (
        <p
          role={notice.ok ? 'status' : 'alert'}
          data-testid={notice.ok ? 'passkeys.recovery.ok' : 'passkeys.recovery.error'}
        >
          {notice.text}
        </p>
      )}
      <p className="text-sm text-gray-700">{t('passkeys.recovery.rotation')}</p>
    </form>
  );
}
