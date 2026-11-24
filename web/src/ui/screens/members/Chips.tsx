// The workgroup chips of one row (a member's access, a device's workgroups): bounded by the org's workgroups, of which
// the first `CHIP_LIMIT` are shown and the rest behind "N more – show" (ADR-063 section 1: no row grows without
// bound until the user asks for it).
import { type ReactNode, useState } from 'react';
import { formatNumber } from '../../../core/i18n';
import { useI18n } from '../../i18n';

/** Chips shown before "N more – show". */
export const CHIP_LIMIT = 8;

const CHIP = 'flex items-center gap-1 rounded border border-gray-200 px-2';

export function Chips<T>(props: {
  readonly items: readonly T[];
  /** The workgroup of a chip (its key and `data-wg`). */
  readonly wgOf: (item: T) => string;
  readonly render: (item: T) => ReactNode;
  readonly testId: string;
  /** Catalog key of the "N more – show" button (with `{count}`). */
  readonly more: string;
  readonly label?: string;
  /** Shown as the only chip when there are none. */
  readonly empty?: ReactNode;
}) {
  const { items, wgOf } = props;
  const { t, locale } = useI18n();
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(0, CHIP_LIMIT);
  const hidden = items.length - shown.length;
  return (
    <ul className="flex flex-wrap gap-2 text-sm" aria-label={props.label} data-testid={props.testId}>
      {items.length === 0 && props.empty}
      {shown.map((item) => (
        <li key={wgOf(item)} className={CHIP} data-wg={wgOf(item)}>
          {props.render(item)}
        </li>
      ))}
      {hidden > 0 && (
        <li>
          <button
            type="button"
            className="min-h-11 min-w-11 px-2 text-blue-800 underline"
            onClick={() => setAll(true)}
            data-testid={`${props.testId}.more`}
          >
            {t(props.more, { count: formatNumber(hidden, locale) })}
          </button>
        </li>
      )}
    </ul>
  );
}
