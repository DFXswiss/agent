// The text of a failed identity change (ADR-076 section 3): `409 peer_outdated` says that some devices of the
// organization must be updated first, with their names when the hub names them; any other code its reason.
import { outdatedDevices } from '../../../core/github/identities';
import { requestErrorCode } from '../../../core/requests/client';
import { useI18n } from '../../i18n';
import { useReason } from '../../settings/sections/github/reason';
import { useDeviceNames } from '../devices/labels';

export function ChangeFailed({
  org,
  error,
  testId,
}: {
  readonly org: string;
  readonly error: unknown;
  readonly testId: string;
}) {
  const { t } = useI18n();
  const reason = useReason();
  const deviceName = useDeviceNames([org]);
  const code = requestErrorCode(error);
  const devices = outdatedDevices(error);
  let text: string;
  if (code !== 'peer_outdated') text = t('identities.failed', { reason: reason(code) });
  else if (devices === null) text = t('identities.outdated');
  else text = t('identities.outdated.devices', { devices: devices.map(deviceName).join(', ') });
  return (
    <p role="alert" className="text-sm text-red-800" data-testid={testId} data-code={code}>
      {text}
    </p>
  );
}
