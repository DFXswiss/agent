// The mailbox of the Claude login automation (06-web.md section 8.2, ADR-011): IMAP host, port, user, password and the
// subscription's e-mail address, stored as the `mailbox` credential (CK-encrypted). The password is never shown again;
// a stored mailbox is named by its address and host. "Check mailbox" asks an execution device to log in once.
import { type FormEvent, useState } from 'react';
import {
  MAILBOX_FIELDS as FIELDS,
  type MailboxForm,
  mailboxBody,
  putCredential,
} from '../../../../core/credentials/records';
import { requestErrorCode } from '../../../../core/requests/client';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';
import { CheckFlow } from './CheckFlow';
import { BUTTON, PRIMARY } from './DevicePicker';

type Status = 'idle' | 'saving' | 'saved' | { readonly code: string };

const EMPTY: MailboxForm = {
  imap_host: '',
  imap_port: '993',
  imap_user: '',
  imap_password: '',
  email: '',
};

export function Mailbox({ stored, onSaved }: { readonly stored: boolean; readonly onSaved: () => void }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [values, setValues] = useState(EMPTY);
  const [status, setStatus] = useState<Status>('idle');
  const [checking, setChecking] = useState(false);
  const body = mailboxBody(values);
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setStatus('saving');
    try {
      await putCredential(app.tasks, 'mailbox', body);
      setValues(EMPTY);
      setStatus('saved');
      onSaved();
    } catch (error) {
      setStatus({ code: requestErrorCode(error) });
    }
  };
  return (
    <div className="flex flex-col gap-2" data-testid="mailbox">
      <h3 className="font-semibold">{t('credentials.mailbox.title')}</h3>
      <p className="text-sm text-gray-600">{t('credentials.mailbox.intro')}</p>
      <form className="grid gap-2 sm:grid-cols-2" onSubmit={(event) => void save(event)}>
        {FIELDS.map((field) => (
          <label key={field} className="flex flex-col gap-1">
            {t(`credentials.mailbox.${field}`)}
            <input
              type={field === 'imap_password' ? 'password' : field === 'imap_port' ? 'number' : 'text'}
              className="min-h-11 rounded border border-gray-400 px-3"
              autoComplete="off"
              value={values[field]}
              onChange={(event) => setValues({ ...values, [field]: event.target.value })}
              data-testid={`mailbox.${field}`}
            />
          </label>
        ))}
        <button
          type="submit"
          className={PRIMARY}
          disabled={body === null || status === 'saving'}
          data-testid="mailbox.save"
        >
          {stored ? t('credentials.mailbox.replace') : t('credentials.mailbox.save')}
        </button>
      </form>
      {status === 'saved' && (
        <p role="status" data-testid="mailbox.saved">
          {t('credentials.mailbox.saved')}
        </p>
      )}
      {typeof status === 'object' && (
        <p role="alert" data-testid="mailbox.failed" data-code={status.code}>
          {t('credentials.save-failed', { reason: reason(status.code) })}
        </p>
      )}
      {stored && !checking && (
        <button type="button" className={BUTTON} onClick={() => setChecking(true)} data-testid="mailbox.check">
          {t('credentials.mailbox.check')}
        </button>
      )}
      {checking && <CheckFlow kind="mailbox" testId="mailbox.check-flow" onClose={() => setChecking(false)} />}
    </div>
  );
}
