// The execution device of a device request of the credentials section (06-web.md section 8.2): the same choice as the
// GitHub connect (`connectDevices`: automatic assignment of a fresh org first, devices the verified logs confirm, the
// online one of the default workgroup preselected, offline ones disabled), then `onPick` with the chosen device.
// `children` adds controls to the choice (the login mode).
import { type ReactNode, useEffect, useState } from 'react';
import { connectDevices, type DeviceChoice } from '../../../../core/github/connection';
import { requestErrorCode } from '../../../../core/requests/client';
import { useI18n } from '../../../i18n';
import { useProfileDeviceNames } from '../../../screens/devices/labels';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';

export const BUTTON = 'min-h-11 self-start rounded border border-gray-400 px-4';
export const PRIMARY = 'min-h-11 self-start rounded bg-blue-700 px-4 text-white';

type Phase =
  | { readonly kind: 'loading' }
  | { readonly kind: 'choose'; readonly devices: readonly DeviceChoice[]; readonly selected: string }
  | { readonly kind: 'none' }
  | { readonly kind: 'error'; readonly code: string };

export function DevicePicker({
  testId,
  startLabel,
  onPick,
  children,
}: {
  readonly testId: string;
  readonly startLabel: string;
  readonly onPick: (device: string) => void;
  readonly children?: ReactNode;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const deviceName = useProfileDeviceNames();
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' });
  useEffect(() => {
    if (phase.kind !== 'loading') return;
    // the picker is unmounted by its flow's "Close": a late answer then changes nothing that is shown
    connectDevices(app.tasks).then(
      (found) =>
        setPhase(
          found.preselected === null
            ? { kind: 'none' }
            : { kind: 'choose', devices: found.devices, selected: found.preselected },
        ),
      (error: unknown) => setPhase({ kind: 'error', code: requestErrorCode(error) }),
    );
  }, [app, phase]);
  const retry = () => setPhase({ kind: 'loading' });
  if (phase.kind === 'loading')
    return (
      <p aria-live="polite" data-testid={`${testId}.devices.loading`}>
        {t('credentials.devices.loading')}
      </p>
    );
  if (phase.kind !== 'choose')
    return (
      <div className="flex flex-col gap-2">
        <p role="alert" data-testid={`${testId}.devices.failed`} data-code={phase.kind === 'error' ? phase.code : ''}>
          {phase.kind === 'error'
            ? t('credentials.devices.failed', { reason: reason(phase.code) })
            : t('credentials.devices.none')}
        </p>
        <button type="button" className={BUTTON} onClick={retry} data-testid={`${testId}.devices.retry`}>
          {t('credentials.retry')}
        </button>
      </div>
    );
  const id = `${testId}-device`;
  return (
    <div className="flex flex-col gap-2">
      <label htmlFor={id}>{t('credentials.devices.label')}</label>
      <select
        id={id}
        className="min-h-11 rounded border border-gray-400 px-3"
        value={phase.selected}
        onChange={(event) => setPhase({ ...phase, selected: event.target.value })}
        data-testid={`${testId}.devices.select`}
      >
        {phase.devices.map((device) => {
          const label = t('credentials.devices.option', { device: deviceName(device.device) });
          return (
            <option key={device.device} value={device.device} disabled={!device.online}>
              {device.online ? label : t('credentials.devices.offline', { label })}
            </option>
          );
        })}
      </select>
      {children}
      <button type="button" className={PRIMARY} onClick={() => onPick(phase.selected)} data-testid={`${testId}.start`}>
        {startLabel}
      </button>
    </div>
  );
}
