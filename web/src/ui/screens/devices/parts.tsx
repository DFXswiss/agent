// Small parts of the device and platform-invitation screens: a copy button with its "Copied" state, the expiry
// select (1 h – 30 d) and a device's display name.
import { useId, useState } from 'react';
import { EXPIRY_HOURS } from '../../../core/org/invites';
import { useI18n } from '../../i18n';
import { BUTTON } from '../members/ActionDialog';
import { expiryLabel } from '../members/InviteSection';

const clipboardWrite = (text: string) => navigator.clipboard.writeText(text);

/** A device's label, or "Device without a name" (ADR-067). */
export function deviceName(
  t: (key: string, params?: Record<string, string>) => string,
  row: { id: string; label: string | null },
) {
  return row.label ?? t('names.device.unknown');
}

/** Copies `text`; the label turns into `copiedLabel` once the clipboard took it. */
export function CopyButton(props: {
  readonly text: string;
  readonly label: string;
  readonly copiedLabel: string;
  readonly testId: string;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className={BUTTON}
      onClick={() =>
        clipboardWrite(props.text).then(
          () => setCopied(true),
          () => setCopied(false),
        )
      }
      data-testid={props.testId}
    >
      {copied ? props.copiedLabel : props.label}
    </button>
  );
}

/** The expiry choice of an invitation. */
export function ExpirySelect(props: {
  readonly label: string;
  readonly value: number;
  readonly onChange: (hours: number) => void;
  readonly testId: string;
  readonly disabled?: boolean;
}) {
  const { t } = useI18n();
  const id = useId();
  return (
    <label htmlFor={id} className="flex flex-col gap-1">
      <span className="font-medium">{props.label}</span>
      <select
        id={id}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={props.value}
        disabled={props.disabled === true}
        onChange={(event) => props.onChange(Number(event.target.value))}
        data-testid={props.testId}
      >
        {EXPIRY_HOURS.map((value) => {
          const label = expiryLabel(value);
          return (
            <option key={value} value={value}>
              {t(label.key, { count: label.count })}
            </option>
          );
        })}
      </select>
    </label>
  );
}
