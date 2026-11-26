// The working modes of a new task (ADR-098 sections 2 and 3): the field "Working modes" – empty, never preselected and
// never taken over from an earlier task (E-32) – lists the hub's catalogue with the titles in the user's language and
// the summary as help text. A mode whose needed modes are not chosen says so and offers to choose them together in one
// click (E-05); nothing is added without that click. Shown only when the hub offers modes.
import { useId } from 'react';
import { missingNeeds, modeTitle, withNeeds } from '../../core/modes/modes';
import { useI18n } from '../i18n';
import { useModeCatalogue } from '../modes/catalogue';

export function ModeChoice({
  value,
  onChange,
  disabled,
}: {
  readonly value: readonly string[];
  readonly onChange: (value: readonly string[]) => void;
  readonly disabled: boolean;
}) {
  const { t, locale } = useI18n();
  const hint = useId();
  const { catalogue } = useModeCatalogue(true, true);
  if (catalogue.length === 0) return null;
  const toggle = (name: string, checked: boolean) =>
    onChange(checked ? [...value, name] : value.filter((item) => item !== name));
  return (
    <fieldset className="flex flex-col gap-1" aria-describedby={hint} data-testid="composer.modes">
      <legend className="text-sm">{t('modes.field.label')}</legend>
      <p id={hint} className="text-xs text-gray-600">
        {t('modes.field.hint')}
      </p>
      <ul className="flex flex-col gap-1">
        {catalogue.map((entry) => {
          const missing = missingNeeds(catalogue, value, entry.name);
          const names = missing.map((name) => modeTitle(catalogue, name, locale)).join(', ');
          return (
            <li key={entry.name} className="flex flex-col" data-testid="composer.mode" data-mode={entry.name}>
              <label className="flex min-h-11 items-start gap-2">
                <input
                  type="checkbox"
                  className="mt-1 h-5 w-5"
                  checked={value.includes(entry.name)}
                  disabled={disabled}
                  onChange={(event) => toggle(entry.name, event.target.checked)}
                  data-testid="composer.mode.check"
                />
                <span className="flex flex-col">
                  <span>{entry.title[locale]}</span>
                  <span className="text-xs text-gray-600">{entry.summary[locale]}</span>
                </span>
              </label>
              {missing.length > 0 && (
                <p className="flex flex-wrap items-center gap-2 pl-7 text-sm" data-testid="composer.mode.needs">
                  <span>{t('modes.needs', { modes: names })}</span>
                  <button
                    type="button"
                    className="min-h-11 rounded border border-gray-300 px-3"
                    disabled={disabled}
                    onClick={() => onChange(withNeeds(catalogue, value, entry.name))}
                    data-testid="composer.mode.with-needs"
                  >
                    {t('modes.needs.add', { modes: names })}
                  </button>
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </fieldset>
  );
}
