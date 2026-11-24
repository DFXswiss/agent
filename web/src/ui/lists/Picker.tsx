// A searchable picker with a bounded result list (ADR-086 section 1) for the choices of a dialog that grow with the
// organization (members, devices, invitees, device candidates): it renders at most `limit` rows. When there are more
// items, a search field filters them (case-insensitive, anywhere in the item's text) and a note says how many match
// ("Showing 20 of 312 – type to narrow the list"). The choices themselves live in the dialog's state, so an item that
// is filtered out keeps its choice. Not a `WindowedList`: a dialog control is searched, not scrolled through.
import { type ComponentType, Fragment, useMemo, useState } from 'react';
import { formatNumber } from '../../core/i18n';
import { useI18n } from '../i18n';
import type { RowProps } from './WindowedList';

/** The most rows a picker renders by default. */
export const PICKER_LIMIT = 20;

export interface PickerProps<T> {
  readonly items: readonly T[];
  readonly getKey: (item: T) => string;
  /** The text a search matches. */
  readonly text: (item: T) => string;
  /** One choice (a stable component, defined at module level). */
  readonly Row: ComponentType<RowProps<T>>;
  /** Prefix of the test ids: `<testId>.search`, `<testId>.more`, `<testId>.none`. */
  readonly testId: string;
  /** Accessible name of the search field. */
  readonly label: string;
  readonly limit?: number;
}

export function Picker<T>({ items, getKey, text, Row, testId, label, limit = PICKER_LIMIT }: PickerProps<T>) {
  const { t, locale } = useI18n();
  const [query, setQuery] = useState('');
  const needle = query.trim().toLocaleLowerCase(locale);
  const matches = useMemo(
    () => (needle === '' ? items : items.filter((item) => text(item).toLocaleLowerCase(locale).includes(needle))),
    [items, text, needle, locale],
  );
  const shown = matches.slice(0, limit);
  return (
    <>
      {items.length > limit && (
        <input
          type="search"
          className="min-h-11 rounded border border-gray-300 px-2"
          aria-label={label}
          placeholder={t('lists.picker.search')}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          data-testid={`${testId}.search`}
        />
      )}
      {shown.map((item) => (
        <Fragment key={getKey(item)}>
          <Row item={item} />
        </Fragment>
      ))}
      {matches.length === 0 && (
        <p className="text-sm text-gray-700" data-testid={`${testId}.none`}>
          {t('lists.picker.none')}
        </p>
      )}
      {matches.length > shown.length && (
        <p className="text-sm text-gray-700" data-testid={`${testId}.more`}>
          {t('lists.picker.more', {
            shown: formatNumber(shown.length, locale),
            total: formatNumber(matches.length, locale),
          })}
        </p>
      )}
    </>
  );
}
