// Add or change a mailbox (KOR-74, ADR-099 section 2): name (unique among the own mailboxes), purpose, address, IMAP
// server, port (993 by default), user and password. The password is write-only: a change shows it empty and keeps the
// stored one while it stays empty.
import { type FormEvent, useState } from 'react';
import {
  addMailbox,
  EMPTY_FORM,
  formOf,
  formReady,
  type Mailbox,
  type MailboxForm as Form,
  nameTaken,
  PURPOSE_MAX,
  updateMailbox,
} from '../../../../core/credentials/mailboxes';
import { requestErrorCode } from '../../../../core/requests/client';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { BUTTON, PRIMARY } from '../credentials/DevicePicker';
import { useReason } from '../github/reason';

const FIELDS = ['name', 'purpose', 'email', 'imap_host', 'imap_port', 'imap_user', 'imap_password'] as const;
type Field = (typeof FIELDS)[number];

const TYPES: Partial<Record<Field, string>> = { imap_port: 'number', imap_password: 'password', email: 'email' };
const LIMITS: Partial<Record<Field, number>> = { name: 64, purpose: PURPOSE_MAX };

export function MailboxForm({
  item,
  items,
  onSaved,
  onClose,
}: {
  /** The mailbox to change, null to add one. */
  readonly item: Mailbox | null;
  readonly items: readonly Mailbox[];
  readonly onSaved: () => void;
  readonly onClose: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [values, setValues] = useState<Form>(item === null ? EMPTY_FORM : formOf(item));
  const [state, setState] = useState<{ readonly kind: 'idle' | 'saving' } | { kind: 'error'; code: string }>({
    kind: 'idle',
  });
  const id = item?.id ?? null;
  const taken = nameTaken(items, values.name, id);
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setState({ kind: 'saving' });
    try {
      if (id === null) await addMailbox(app.tasks, values, [], items);
      else await updateMailbox(app.tasks, id, values, items);
      onSaved();
    } catch (error) {
      setState({ kind: 'error', code: requestErrorCode(error) });
    }
  };
  const prefix = `mbform-${id ?? 'new'}`;
  const label = (field: Field) =>
    field === 'imap_password' && id !== null ? t('mailboxes.imap_password.keep') : t(`mailboxes.${field}`);
  return (
    <form className="flex flex-col gap-2" onSubmit={(event) => void save(event)} data-testid="mbform">
      <div className="grid gap-2 sm:grid-cols-2">
        {FIELDS.map((field) => (
          <label key={field} className="flex flex-col gap-1" htmlFor={`${prefix}-${field}`}>
            {label(field)}
            <input
              id={`${prefix}-${field}`}
              type={TYPES[field] ?? 'text'}
              maxLength={LIMITS[field]}
              className="min-h-11 rounded border border-gray-400 px-3"
              autoComplete="off"
              value={values[field]}
              onChange={(event) => setValues({ ...values, [field]: event.target.value })}
              data-testid={`mbform.${field}`}
            />
          </label>
        ))}
      </div>
      <p className="text-sm text-gray-600">{t('mailboxes.name.hint')}</p>
      {taken && (
        <p role="alert" data-testid="mbform.name.taken">
          {t('mailboxes.name.taken')}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          className={PRIMARY}
          disabled={!formReady(values, items, id) || state.kind === 'saving'}
          data-testid="mbform.save"
        >
          {t('mailboxes.save')}
        </button>
        <button type="button" className={BUTTON} onClick={onClose} data-testid="mbform.cancel">
          {t('mailboxes.cancel')}
        </button>
      </div>
      {state.kind === 'error' && (
        <p role="alert" data-testid="mbform.failed" data-code={state.code}>
          {t('mailboxes.save-failed', { reason: reason(state.code) })}
        </p>
      )}
    </form>
  );
}
