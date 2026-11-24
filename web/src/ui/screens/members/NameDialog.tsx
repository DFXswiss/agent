// "Enter name" / "Change name" (ADR-067): an admin gives a member without an own name – or the person of an
// invitation – a provisional name (`member.name`, sealed with the org key). Every member sees it until the person's
// own name is written.
import { useId, useState } from 'react';
import { displayName } from '../../../core/account/genesis';
import { giveName } from '../../../core/org/names';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { ActionDialog } from './ActionDialog';

export function NameDialog({
  org,
  subject,
  current,
  invite = false,
  onClose,
}: {
  readonly org: string;
  readonly subject: string;
  readonly current: string;
  readonly invite?: boolean;
  readonly onClose: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const id = useId();
  const [text, setText] = useState(current);
  const valid = displayName(text) !== null;
  return (
    <ActionDialog
      title={t(invite ? 'names.give.title.invite' : 'names.give.title')}
      testId="members.name"
      confirmLabel={t('names.give.confirm')}
      ready={valid}
      run={() => giveName(app.tasks, org, subject, text)}
      onDone={onClose}
      onClose={onClose}
    >
      <label htmlFor={id} className="flex flex-col gap-1 font-medium">
        {t('names.give.label')}
        <input
          id={id}
          className="min-h-11 rounded border border-gray-300 px-2"
          value={text}
          maxLength={200}
          autoComplete="off"
          onChange={(event) => setText(event.target.value)}
          data-testid="members.name.input"
        />
      </label>
      <p className="text-sm text-gray-700">{valid || text === '' ? t('names.give.hint') : t('names.give.invalid')}</p>
    </ActionDialog>
  );
}
