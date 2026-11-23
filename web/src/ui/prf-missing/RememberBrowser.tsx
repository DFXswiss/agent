// "Remember this browser" offered right after the recovery code when the new passkey gave no PRF output (06-web.md
// section 3.3, last paragraph): without it every sign-in on this browser needs the recovery code. Choosing it stores a
// non-extractable device key in IndexedDB that encrypts the identity (`core/persist/device-key.ts`); "Not now" goes on.
import { useState } from 'react';
import type { Identity } from '../../core/crypto/keys';
import type { DeviceKeys } from '../../core/persist/device-key';
import { useI18n } from '../i18n';
import { deviceKeys, rememberBrowser } from './device';

export interface RememberBrowserProps {
  readonly userId: string;
  readonly identity: Identity;
  readonly onDone: () => void;
  readonly keys?: () => Promise<DeviceKeys>;
}

export function RememberBrowser({ userId, identity, onDone, keys = deviceKeys }: RememberBrowserProps) {
  const { t } = useI18n();
  const [state, setState] = useState<'idle' | 'busy' | 'failed'>('idle');
  const remember = async () => {
    setState('busy');
    if (await rememberBrowser(userId, identity, keys)) onDone();
    else setState('failed');
  };
  return (
    <section className="flex flex-col gap-3" data-testid="prf-missing.offer">
      <h2 className="text-xl font-semibold">{t('passkeys.prf-missing.offer.title')}</h2>
      <p data-testid="prf-missing.explain">{t('passkeys.prf-missing.explain')}</p>
      <p>{t('passkeys.prf-missing.offer.body')}</p>
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          className="min-h-11 rounded bg-gray-900 px-4 text-white disabled:opacity-60"
          onClick={() => void remember()}
          disabled={state === 'busy'}
          data-testid="prf-missing.offer.remember"
        >
          {t('passkeys.prf-missing.remember')}
        </button>
        <button
          type="button"
          className="min-h-11 rounded border border-gray-400 px-4"
          onClick={onDone}
          disabled={state === 'busy'}
          data-testid="prf-missing.offer.skip"
        >
          {t('passkeys.prf-missing.offer.skip')}
        </button>
      </div>
      {state === 'failed' && (
        <p role="alert" className="text-red-800" data-testid="prf-missing.offer.failed">
          {t('passkeys.prf-missing.offer.failed')}
        </p>
      )}
      <p className="text-sm text-gray-700">{t('passkeys.prf-missing.other')}</p>
    </section>
  );
}
