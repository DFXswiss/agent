// A verified `device.profile` event in an org scope (what the pulled device profiles give the UI's device names).
import { putEvents } from '../../src/core/store/slices/events';
import type { Store } from '../../src/core/store';

let seq = 90_000;

export function labelDevice(store: Store, org: string, device: string, label: string): void {
  seq += 1;
  putEvents(store, org, [
    {
      org,
      scopeId: org,
      scopeSeq: seq,
      receivedAt: null,
      event: {},
      status: 'verified',
      code: null,
      header: { id: `device-profile-${seq}`, kind: 'device.profile', ref: device },
      body: { label },
    },
  ]);
}
