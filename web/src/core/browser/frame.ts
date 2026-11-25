// Opening a live frame of a watched session (04-crypto.md section 4.13, 06-web.md section 5.4b): the frame's format,
// session and workgroup are checked (`checkFrame`), the workgroup key of the frame's epoch is taken from the keys this
// user holds (`keys_pending` without it) and the JPEG is opened (`openFrame`: size, decryption).
import { checkFrame, openFrame } from '../crypto/browserframe';
import { parseTime } from '../encoding/time';
import { epochKey, WG } from '../log/state';
import { RequestError } from '../requests/client';
import { orgEntry } from '../store/slices/orgs';
import type { Store } from '../store/store';
import type { FrameOpener } from './live';

/** The opener of the live frames of `session` in workgroup `wg` of `org`. */
export function frameOpener(store: Store, org: string, wg: string, session: string): FrameOpener {
  return async (frame) => {
    // the checked frame has its formats: a `br-` ID, an epoch, a seq and a time
    const checked = checkFrame(frame, { session, wg });
    const key = orgEntry(store.getState().orgs, org).keys.get(epochKey(WG, wg, checked.epoch as number));
    if (key === undefined) throw new RequestError('keys_pending');
    const jpeg = await openFrame(key, frame, { session, wg });
    return { browser: checked.browser as string, seq: checked.seq as number, ts: parseTime(checked.ts), jpeg };
  };
}
