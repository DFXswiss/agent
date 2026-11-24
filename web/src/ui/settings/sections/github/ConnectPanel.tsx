// A connect panel for GitHub identities (KOR-47, ADR-076): the two ways of `ConnectFlow` (device flow, existing token)
// with the option "also create a signing key", for adding a further own account, reconnecting a listed one and
// connecting a company account of an organization. The connection itself is `ConnectFlow` with `options`;
// `complete` (company accounts) runs on `done`. Buttons of 44 px; the panel stacks on a phone.
import { useId, useState } from 'react';
import type { ConnectOptions } from '../../../../core/github/connection';
import type { RequestView } from '../../../../core/requests/client';
import { useI18n } from '../../../i18n';
import { type ConnectMethod, ConnectFlow } from './ConnectFlow';

const BUTTON = 'min-h-11 rounded border border-gray-400 px-4';

export function ConnectPanel({
  title,
  options,
  complete,
  onConnected,
  onClose,
  testId,
}: {
  readonly title: string;
  readonly options: Omit<ConnectOptions, 'signingKey'>;
  readonly complete?: (view: RequestView) => Promise<void>;
  readonly onConnected: () => void;
  readonly onClose: () => void;
  readonly testId: string;
}) {
  const { t } = useI18n();
  const id = useId();
  const [signing, setSigning] = useState(false);
  const [method, setMethod] = useState<ConnectMethod | null>(null);
  if (method !== null)
    return (
      <ConnectFlow
        method={method}
        options={{ ...options, signingKey: signing }}
        onConnected={onConnected}
        onClose={onClose}
        {...(complete !== undefined && { complete })}
      />
    );
  return (
    <section className="flex flex-col gap-3 rounded border border-gray-300 p-4" data-testid={testId} aria-label={title}>
      <h3 className="font-semibold">{title}</h3>
      <div className="flex min-h-11 items-center gap-2">
        <input
          id={id}
          type="checkbox"
          className="h-5 w-5"
          checked={signing}
          onChange={(event) => setSigning(event.target.checked)}
          data-testid={`${testId}.signing`}
        />
        <label htmlFor={id}>{t('identities.signing-key')}</label>
      </div>
      <p className="text-sm text-gray-700">{t('identities.signing-key.hint')}</p>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="min-h-11 rounded bg-blue-700 px-4 text-white"
          onClick={() => setMethod('device_flow')}
          data-testid={`${testId}.device-flow`}
        >
          {t('github.connect')}
        </button>
        <button type="button" className={BUTTON} onClick={() => setMethod('token')} data-testid={`${testId}.token`}>
          {t('github.token.open')}
        </button>
        <button type="button" className={BUTTON} onClick={onClose} data-testid={`${testId}.close`}>
          {t('github.close')}
        </button>
      </div>
    </section>
  );
}
