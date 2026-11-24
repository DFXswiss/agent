// The Anthropic API key (06-web.md section 8.2): a password field; "Save" encrypts the key under the current `CK`
// and stores it (`PUT /v1/me/credentials/anthropic_api_key` with `expected_version`). The field is cleared after
// saving; the stored key is never shown again. "Check key" asks an execution device whether it works.
import { type FormEvent, useState } from 'react';
import { putCredential } from '../../../../core/credentials/records';
import { requestErrorCode } from '../../../../core/requests/client';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';
import { CheckFlow } from './CheckFlow';
import { BUTTON, PRIMARY } from './DevicePicker';

const KEY_RE = /^[!-~]{1,4096}$/;

type Status = 'idle' | 'saving' | 'saved' | { readonly code: string };

export function ApiKey({ stored, onSaved }: { readonly stored: boolean; readonly onSaved: () => void }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [key, setKey] = useState('');
  const [status, setStatus] = useState<Status>('idle');
  const [checking, setChecking] = useState(false);
  const valid = KEY_RE.test(key.trim());
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setStatus('saving');
    try {
      await putCredential(app.tasks, 'anthropic_api_key', { key: key.trim() });
      setKey('');
      setStatus('saved');
      onSaved();
    } catch (error) {
      setStatus({ code: requestErrorCode(error) });
    }
  };
  return (
    <div className="flex flex-col gap-2" data-testid="api-key">
      <h3 className="font-semibold">{t('credentials.api-key.title')}</h3>
      <form className="flex flex-col gap-2" onSubmit={(event) => void save(event)}>
        <label htmlFor="api-key-input">
          {stored ? t('credentials.api-key.replace') : t('credentials.api-key.label')}
        </label>
        <input
          id="api-key-input"
          type="password"
          className="min-h-11 rounded border border-gray-400 px-3 font-mono"
          autoComplete="off"
          spellCheck={false}
          value={key}
          onChange={(event) => setKey(event.target.value)}
          data-testid="api-key.input"
        />
        <button type="submit" className={PRIMARY} disabled={!valid || status === 'saving'} data-testid="api-key.save">
          {t('credentials.api-key.save')}
        </button>
      </form>
      {status === 'saved' && (
        <p role="status" data-testid="api-key.saved">
          {t('credentials.api-key.saved')}
        </p>
      )}
      {typeof status === 'object' && (
        <p role="alert" data-testid="api-key.failed" data-code={status.code}>
          {t('credentials.save-failed', { reason: reason(status.code) })}
        </p>
      )}
      {stored && !checking && (
        <button type="button" className={BUTTON} onClick={() => setChecking(true)} data-testid="api-key.check">
          {t('credentials.api-key.check')}
        </button>
      )}
      {checking && (
        <CheckFlow kind="anthropic_api_key" testId="api-key.check-flow" onClose={() => setChecking(false)} />
      )}
    </div>
  );
}
