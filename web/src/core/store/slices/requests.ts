// Slice `requests`: the newest response `seq` the live channel announced per device request (`device_response
// {request_id, seq}` frames, 03-api.md section 5). The frame is only a hint: a flow that waits for a request pulls
// `GET /v1/device-requests/{id}` when the number grows (and on its own timer, push may be lost).
import { isRecord } from '../../registry';
import type { Slice, Store } from '../store';

export interface RequestsState {
  /** Request ID → newest announced response `seq`. */
  readonly seqs: Readonly<Record<string, number>>;
}

declare module '../store' {
  interface StoreState {
    requests: RequestsState;
  }
}

export const slice: Slice<'requests'> = { name: 'requests', initial: () => ({ seqs: {} }) };

/** Records an announced response (a lower `seq` than known changes nothing). */
export function noteResponse(store: Store, requestId: string, seq: number): void {
  store.update('requests', (state) =>
    (state.seqs[requestId] ?? 0) >= seq ? state : { seqs: { ...state.seqs, [requestId]: seq } },
  );
}

export const frames = {
  device_response: (store: Store, data: unknown) => {
    if (isRecord(data) && typeof data.request_id === 'string' && Number.isSafeInteger(data.seq))
      noteResponse(store, data.request_id, data.seq as number);
  },
};
