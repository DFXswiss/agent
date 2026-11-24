// The composer's device choice (06-web.md section 4.1, ADR-075 section 6): "Device: Automatic" or one of the devices
// assigned to the workgroup (`GET /v1/workgroups/{wg}`), and for a chosen device "Any VM" or one of the workgroup's
// live VMs there with its sessions ("vm-1a2b3c4d · 3 of 16 sessions"). Preselected with the workgroup's default; a
// default whose device is no longer listed counts as automatic. Without any device the choice is not shown.
import { useEffect, useId, useState } from 'react';
import { sessionsPerVm } from '../../core/org/devices';
import { loadWorkgroupDevices, type Placement, validChoice, type WorkgroupDevices } from '../../core/tasks/placement';
import { useI18n } from '../i18n';
import { useDeviceNames } from '../screens/devices/labels';
import { useAppEnv } from '../shell/context';

export const AUTO = 'auto';
export const ANY_VM = 'any';

/** The devices of `wg` (null while loading or after a failure: the choice stays automatic). */
export function useWorkgroupDevices(wg: string, refresh = 0): WorkgroupDevices | null {
  const { app } = useAppEnv();
  const [loaded, setLoaded] = useState<{ wg: string; devices: WorkgroupDevices | null } | null>(null);
  useEffect(() => {
    let current = true;
    loadWorkgroupDevices(app.tasks, wg).then(
      (found) => current && setLoaded({ wg, devices: found }),
      () => current && setLoaded({ wg, devices: null }),
    );
    return () => {
      current = false;
    };
  }, [app, wg, refresh]);
  return loaded?.wg === wg ? loaded.devices : null;
}

export interface PlacementSelectProps {
  readonly org: string;
  readonly devices: WorkgroupDevices;
  readonly value: Placement | null;
  readonly onChange: (placement: Placement | null) => void;
  /** `data-testid` prefix of the two selects (`<prefix>.device`, `<prefix>.vm`). */
  readonly testId: string;
  readonly disabled?: boolean;
}

/** The two selects "Device" and "VM" over the devices of a workgroup. */
export function PlacementSelect({ org, devices, value, onChange, testId, disabled = false }: PlacementSelectProps) {
  const { t } = useI18n();
  const name = useDeviceNames([org]);
  const deviceId = useId();
  const vmId = useId();
  const chosen = value === null ? undefined : devices.devices.find((item) => item.device_id === value.device_id);
  const limit = chosen === undefined ? null : sessionsPerVm(chosen.capacity);
  const vmValue = value === null || value.vm_id === null ? ANY_VM : value.vm_id;
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <label htmlFor={deviceId}>{t('home.device.label')}</label>
      <select
        id={deviceId}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={value?.device_id ?? AUTO}
        disabled={disabled}
        onChange={(event) =>
          onChange(event.target.value === AUTO ? null : { device_id: event.target.value, vm_id: null })
        }
        data-testid={`${testId}.device`}
      >
        <option value={AUTO}>{t('home.device.auto')}</option>
        {devices.devices.map((device) => (
          <option key={device.device_id} value={device.device_id}>
            {device.online ? name(device.device_id) : t('home.device.offline', { name: name(device.device_id) })}
          </option>
        ))}
      </select>
      {chosen !== undefined && (
        <>
          <label htmlFor={vmId}>{t('home.vm.label')}</label>
          <select
            id={vmId}
            className="min-h-11 rounded border border-gray-300 px-2"
            value={vmValue}
            disabled={disabled}
            onChange={(event) =>
              onChange({
                device_id: chosen.device_id,
                vm_id: event.target.value === ANY_VM ? null : event.target.value,
              })
            }
            data-testid={`${testId}.vm`}
          >
            <option value={ANY_VM}>{t('home.vm.any')}</option>
            {chosen.vms.map((vm) => (
              <option key={vm.vm_id} value={vm.vm_id}>
                {limit === null
                  ? t('home.vm.sessions', { vm: vm.vm_id, sessions: vm.sessions })
                  : t('home.vm.sessions-of', { vm: vm.vm_id, sessions: vm.sessions, limit })}
              </option>
            ))}
          </select>
        </>
      )}
    </div>
  );
}

/** The composer's choice: preselected with the workgroup default; `null` = automatic. */
export function useDeviceChoice(wg: string): {
  readonly devices: WorkgroupDevices | null;
  readonly choice: Placement | null;
  readonly setChoice: (placement: Placement | null) => void;
} {
  const devices = useWorkgroupDevices(wg);
  const [picked, setPicked] = useState<{ wg: string; placement: Placement | null } | null>(null);
  const preselected = devices === null ? null : validChoice(devices.devices, devices.placement);
  const choice = picked?.wg === wg ? picked.placement : preselected;
  return { devices, choice, setChoice: (placement) => setPicked({ wg, placement }) };
}
