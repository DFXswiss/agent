// The GitHub identity of a new task (ADR-076 section 3): "Automatic" (the workgroup's or organization's default, else
// the user's primary account, else the operator's) or one of the identities a task of the organization may choose –
// the user's own accounts and the company accounts of the organization. Shown only when there is a choice.
import { useId } from 'react';
import { usableIdentities } from '../../core/github/identities';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { useIdentities, useIdentityText } from '../settings/sections/github/identities';

export const AUTOMATIC = '';

export function IdentityChoice({
  org,
  orgName,
  value,
  onChange,
  disabled = false,
  testId,
}: {
  readonly org: string;
  readonly orgName: string | null;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly disabled?: boolean;
  readonly testId: string;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const id = useId();
  const userId = useStore((state) => state.me.userId, app.store);
  const { state } = useIdentities();
  const text = useIdentityText(state.knownLogins, orgName);
  const usable = usableIdentities(state.identities, userId, org);
  if (usable.length === 0) return null;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm">
        {t('identities.task.label')}
      </label>
      <select
        id={id}
        className="min-h-11 rounded border border-gray-400 px-3"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        data-testid={testId}
      >
        <option value={AUTOMATIC}>{t('identities.task.automatic')}</option>
        {usable.map((item) => (
          <option key={item.id} value={item.id}>
            {text(item)}
          </option>
        ))}
      </select>
    </div>
  );
}
