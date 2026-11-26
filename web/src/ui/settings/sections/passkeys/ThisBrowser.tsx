// "Stay signed in" on this browser profile (06-web.md sections 3.3 and 3.4, rule E-40): with a device-key entry the
// browser stays signed in and opens the data without a passkey – shown with its date and "Forget on this browser"
// (deletes the IndexedDB entry; the session then simply ends, a reload asks for the passkey); without one "Stay signed
// in on this browser" (a new non-extractable device key encrypts the identity in memory). Only the signed-in user's
// entry is shown.
import { useCallback, useEffect, useState } from 'react';
import type { DeviceInfo, DeviceKeys } from '../../../../core/persist/device-key';
import { formatEventTime } from '../../../../core/i18n';
import { useStore } from '../../../hooks/useStore';
import { useNow } from '../../../countdown/useNow';
import { useI18n } from '../../../i18n';
import { deviceKeys, rememberBrowser } from '../../../prf-missing/device';
import { useAppEnv } from '../../../shell/context';

type State =
  | { readonly step: 'loading' }
  | { readonly step: 'ready'; readonly info: DeviceInfo | null }
  | { readonly step: 'failed' };

export function ThisBrowser({ keys = deviceKeys }: { readonly keys?: () => Promise<DeviceKeys> }) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const userId = useStore((state) => state.me.userId, app.store)!;
  const identity = useStore((state) => state.me.identity, app.store);
  const [state, setState] = useState<State>({ step: 'loading' });
  const [busy, setBusy] = useState(false);

  const now = useNow(false);
  const load = useCallback(
    () =>
      keys()
        .then((store) => store.info(userId))
        .then(
          (info) => setState({ step: 'ready', info }),
          () => setState({ step: 'failed' }),
        ),
    [keys, userId],
  );
  useEffect(() => {
    void load();
  }, [load]);

  const forget = async () => {
    setBusy(true);
    await (await keys()).forget(userId).catch(() => undefined);
    app.noteRemembered(false);
    await load();
    setBusy(false);
  };
  const remember = async () => {
    setBusy(true);
    if (!(await rememberBrowser(userId, identity!, keys))) setState({ step: 'failed' });
    else {
      app.noteRemembered(true);
      await load();
    }
    setBusy(false);
  };

  return (
    <div className="flex flex-col gap-3" data-testid="passkeys.browser" data-state={state.step}>
      <h3 className="text-lg font-semibold">{t('passkeys.browser.title')}</h3>
      {state.step === 'failed' && (
        <p role="alert" data-testid="passkeys.browser.failed">
          {t('passkeys.browser.failed')}
        </p>
      )}
      {state.step === 'ready' && state.info !== null && (
        <>
          <p data-testid="passkeys.browser.remembered">
            {t('passkeys.browser.remembered', {
              time: formatEventTime(state.info.createdAt, {
                locale,
                timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                now,
              }),
            })}
          </p>
          <button
            type="button"
            className="min-h-11 self-start rounded border border-gray-400 px-4 disabled:opacity-60"
            onClick={() => void forget()}
            disabled={busy}
            data-testid="passkeys.browser.forget"
          >
            {t('passkeys.browser.forget')}
          </button>
        </>
      )}
      {state.step === 'ready' && state.info === null && (
        <>
          <p data-testid="passkeys.browser.none">{t('passkeys.browser.none')}</p>
          <button
            type="button"
            className="min-h-11 self-start rounded border border-gray-400 px-4 disabled:opacity-60"
            onClick={() => void remember()}
            disabled={busy}
            data-testid="passkeys.browser.remember"
          >
            {t('passkeys.browser.remember')}
          </button>
        </>
      )}
    </div>
  );
}
