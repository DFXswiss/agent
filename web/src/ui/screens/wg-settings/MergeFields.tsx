// The merge criteria and the separation of duties of a workgroup (ADR-094 sections 5 and 6; part of the settings
// form): passed verdicts of review sessions on the head, their profile, green checks, approving GitHub reviews and
// separate duties. Every field is optional and off by default: without them only GitHub's own rules decide (E-11).
import { useId } from 'react';
import { mergeValues, type SettingsInput } from '../../../core/wg-settings/settings';
import { useI18n } from '../../i18n';

const FIELD = 'min-h-11 w-24 rounded border border-gray-300 px-2';

export function MergeFields({
  input,
  edit,
}: {
  readonly input: SettingsInput;
  readonly edit: (next: Partial<SettingsInput>) => void;
}) {
  const { t } = useI18n();
  const id = useId();
  const values = mergeValues(input);
  const count = (value: string): number => (value.trim() === '' ? 0 : Number(value));
  return (
    <fieldset className="flex flex-col gap-2 rounded border border-gray-200 p-3" data-testid="wg-settings.merge">
      <legend className="font-medium">{t('wg-settings.merge.legend')}</legend>
      <p className="text-sm text-gray-600">{t('wg-settings.merge.hint')}</p>
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={`${id}-verdicts`}>{t('wg-settings.merge.verdicts')}</label>
        <input
          id={`${id}-verdicts`}
          type="number"
          min={0}
          className={FIELD}
          value={values.verdicts}
          onChange={(event) => edit({ mergeVerdicts: count(event.target.value) })}
          data-testid="wg-settings.merge.verdicts"
        />
        <label htmlFor={`${id}-profile`}>{t('wg-settings.merge.profile')}</label>
        <input
          id={`${id}-profile`}
          className="min-h-11 w-40 rounded border border-gray-300 px-2"
          value={values.profile}
          onChange={(event) => edit({ mergeVerdictProfile: event.target.value })}
          spellCheck={false}
          data-testid="wg-settings.merge.profile"
        />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={`${id}-approvals`}>{t('wg-settings.merge.approvals')}</label>
        <input
          id={`${id}-approvals`}
          type="number"
          min={0}
          className={FIELD}
          value={values.approvals}
          onChange={(event) => edit({ mergeApprovals: count(event.target.value) })}
          data-testid="wg-settings.merge.approvals"
        />
      </div>
      <div className="flex min-h-11 items-center gap-2">
        <input
          id={`${id}-green`}
          type="checkbox"
          className="h-5 w-5"
          checked={values.green}
          onChange={(event) => edit({ mergeChecksGreen: event.target.checked })}
          data-testid="wg-settings.merge.green"
        />
        <label htmlFor={`${id}-green`} className="flex min-h-11 items-center">
          {t('wg-settings.merge.green')}
        </label>
      </div>
      <div className="flex min-h-11 items-center gap-2">
        <input
          id={`${id}-separate`}
          type="checkbox"
          className="h-5 w-5"
          checked={values.separate}
          onChange={(event) => edit({ separateDuties: event.target.checked })}
          data-testid="wg-settings.merge.separate"
        />
        <label htmlFor={`${id}-separate`} className="flex min-h-11 items-center">
          {t('wg-settings.merge.separate')}
        </label>
      </div>
    </fieldset>
  );
}
