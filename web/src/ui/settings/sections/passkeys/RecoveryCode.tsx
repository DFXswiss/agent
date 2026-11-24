// The recovery code in `/settings` (06-web.md section 3.2; KOR-01, KOR-22): whether one exists (the recovery slot of a
// registration without code is the unset slot, `hasRecoveryCode`) – one quiet line while none does – and "Create
// recovery code" (or "Create a new recovery code"): the browser draws the code and seals the unlocked identity with
// it, shows it with "Copy"/"Download" and one "Done"; "Done" proves the user with one passkey gesture and the hub
// stores the slot (`rotateRecoveryCode`). Until the hub accepted it an old code stays valid; a cancelled gesture keeps
// the shown code for another try. Only a code known to exist (`set`) is called "current" or "old"; while the status is
// loading or unreadable the screen speaks as if there were none. No retyping, no reminder elsewhere.
import { useEffect, useState } from 'react';
import { CeremonyCancelled } from '../../../../core/account/ceremony';
import { hasRecoveryCode } from '../../../../core/account/passkeys';
import {
  type NewRecoveryCode as Prepared,
  newRecoveryCode,
  rotateRecoveryCode,
} from '../../../../core/account/recover';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { RecoveryCodeStep } from '../../../screens/invite/RecoveryCodeStep';
import { useAppEnv } from '../../../shell/context';
import { errorCode } from '../../../shell/errors';

/** `loading`: not known yet; `unknown`: the slot could not be read (offline …): no hint, the button still works. */
type Status = 'loading' | 'none' | 'set' | 'unknown';

type Flow =
  | { readonly step: 'idle'; readonly done: 'saved' | 'replaced' | null }
  | { readonly step: 'show'; readonly prepared: Prepared; readonly notice: string | null; readonly busy: boolean };

const BUTTON = 'min-h-11 self-start rounded border border-gray-400 px-4 disabled:opacity-60';

export function RecoveryCode() {
  const { app, credentials } = useAppEnv();
  const { t, error } = useI18n();
  const identity = useStore((state) => state.me.identity, app.store);
  const [status, setStatus] = useState<Status>('loading');
  const [flow, setFlow] = useState<Flow>({ step: 'idle', done: null });

  useEffect(() => {
    if (identity === null) return;
    app
      .recoveryBundle()
      .then((bundle) => hasRecoveryCode(app.userId!, identity, bundle))
      .then(
        (has) => setStatus(has ? 'set' : 'none'),
        () => setStatus('unknown'),
      );
  }, [app, identity]);

  const start = async () => {
    setFlow({ step: 'show', prepared: await newRecoveryCode(app.userId!, identity!), notice: null, busy: false });
  };

  const save = async (current: Extract<Flow, { step: 'show' }>) => {
    setFlow({ ...current, notice: null, busy: true });
    try {
      const request = (method: string, target: string, options?: { body: unknown }) =>
        app.client.request(method, target, options);
      await rotateRecoveryCode(request, credentials, current.prepared);
    } catch (caught) {
      const text =
        caught instanceof CeremonyCancelled ? t('passkeys.recovery.cancelled') : error(errorCode(caught)).title;
      setFlow({ ...current, notice: text, busy: false });
      return;
    }
    setFlow({ step: 'idle', done: status === 'set' ? 'replaced' : 'saved' });
    setStatus('set');
  };

  return (
    <div className="flex flex-col gap-3" data-testid="passkeys.recovery" data-status={status} data-step={flow.step}>
      <h3 className="text-lg font-semibold">{t('passkeys.recovery.title')}</h3>
      {flow.step === 'idle' ? (
        <>
          <p>{t('passkeys.recovery.explain')}</p>
          {status === 'none' && (
            <p className="text-sm text-gray-700" data-testid="passkeys.recovery.none">
              {t('passkeys.recovery.none')}
            </p>
          )}
          <button
            type="button"
            className={BUTTON}
            disabled={identity === null}
            onClick={() => void start()}
            data-testid="passkeys.recovery.create"
          >
            {status === 'set' ? t('passkeys.recovery.renew') : t('passkeys.recovery.create')}
          </button>
          {flow.done !== null && (
            <p role="status" data-testid="passkeys.recovery.done">
              {t(`passkeys.recovery.${flow.done}`)}
            </p>
          )}
        </>
      ) : (
        <>
          <RecoveryCodeStep
            code={flow.prepared.code}
            explain={
              status === 'set'
                ? `${t('passkeys.recovery.show')} ${t('passkeys.recovery.show.old')}`
                : t('passkeys.recovery.show')
            }
            continueLabel={t('passkeys.recovery.finish')}
            onContinue={() => void save(flow)}
            busy={flow.busy}
          >
            {flow.busy && (
              <p role="status" data-testid="passkeys.recovery.working">
                {t('passkeys.recovery.working')}
              </p>
            )}
            {flow.notice !== null && (
              <p role="alert" className="text-red-800" data-testid="passkeys.recovery.notice">
                {flow.notice}
              </p>
            )}
          </RecoveryCodeStep>
          <button
            type="button"
            className={BUTTON}
            disabled={flow.busy}
            onClick={() => setFlow({ step: 'idle', done: null })}
            data-testid="passkeys.recovery.cancel"
          >
            {t('passkeys.recovery.cancel')}
          </button>
        </>
      )}
    </div>
  );
}
