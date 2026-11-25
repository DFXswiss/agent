// The event bodies of the platform browser (13-work-vm.md section 13.5, ADR-085 sections 4 and 7), checked like the
// work VM bodies (`core/chat/shape.ts`: every field present and valid, no other field); anything else is null and the
// web shows a neutral row instead of content:
//
// - `browser.action`: one entry of the action record per tool call (a watched kind: read with `kinds=browser.action`
//   or received as `browser_action` frame while the session's browser is watched). `text` is shown as stored – the
//   runner already replaced a password with "••••". `image` is the viewport after the action (JPEG, b64u);
// The checks follow `contracts/events/objects.json` (`body_browser_action`, `body_browser_state`).
// - `browser.state`: the browser session's state (`starting` before the VM is obtained, `open`, `restarted` after a
//   failure, `closed` with its reason), in the general fan-out like every chat event.
import { isB64u } from '../encoding/b64u';
import { type Check, count, flag, integer, matches, nullable, oneOf, pattern, text, time, uuid } from '../chat/shape';

export const ACTION_KIND = 'browser.action';
export const STATE_KIND = 'browser.state';
/** The operation types of the platform browser (ADR-085 section 1). */
export const OPEN_TYPE = 'browser.open';
export const CLOSE_TYPE = 'browser.close';

export const BROWSER_STATES = ['starting', 'open', 'restarted', 'closed'] as const;
export type BrowserStateName = (typeof BROWSER_STATES)[number];

/** Why a browser session closed or its VM was replaced (13-work-vm.md section 13.2, `browser_close_reason`). */
export const BROWSER_REASONS = [
  'browser_closed',
  'browser_idle',
  'session_ended',
  'task_closed',
  'credit_exhausted',
  'restart',
  'failed',
  'vm_lost',
] as const;
export type BrowserReason = (typeof BROWSER_REASONS)[number];

export const IMAGE_OMITTED = ['too_large', 'unavailable'] as const;

/** The tools of the platform browser (13-work-vm.md section 13.4). */
export const BROWSER_TOOLS = [
  'browser_navigate',
  'browser_snapshot',
  'browser_click',
  'browser_hover',
  'browser_type',
  'browser_select',
  'browser_press',
  'browser_scroll',
  'browser_wait',
  'browser_evaluate',
  'browser_upload',
  'browser_dialog',
  'browser_tabs',
  'browser_download',
  'browser_screenshot',
] as const;

/** At most this many bytes of a picture of the record (13-work-vm.md section 13.5). */
export const IMAGE_BYTES = 100 * 1024;
export const IMAGE_WIDTH = 960;

export interface ActionImage {
  readonly type: 'image/jpeg';
  readonly width: number;
  readonly height: number;
  /** b64u of the JPEG. */
  readonly data: string;
}

export interface BrowserAction {
  readonly browser_id: string;
  readonly seq: number;
  readonly tool: string;
  readonly action: string;
  readonly target: string | null;
  /** What was typed; `"••••"` for a password field (masked by the runner). */
  readonly text: string | null;
  readonly url: string;
  readonly title: string;
  readonly tab: number;
  readonly ok: boolean;
  readonly error: string | null;
  readonly notice: string | null;
  readonly at: string;
  readonly duration_ms: number;
  readonly image: ActionImage | null;
  readonly image_omitted: (typeof IMAGE_OMITTED)[number] | null;
}

export interface BrowserState {
  readonly browser_id: string;
  readonly operation_id: string | null;
  readonly implicit: boolean;
  readonly state: BrowserStateName;
  readonly workvm_id: string | null;
  readonly reason: BrowserReason | null;
  readonly url: string | null;
  readonly actions: number;
  readonly ready_ms: number | null;
  readonly idle_seconds: number;
  readonly opened_at: string | null;
  readonly closed_at: string | null;
}

export const BROWSER_ID_RE = /^br-[0-9a-f]{12}$/;
export const browserId = pattern(BROWSER_ID_RE);

/** The longest b64u text of `IMAGE_BYTES` bytes. */
export const IMAGE_CHARS = Math.ceil((IMAGE_BYTES * 4) / 3);

/** The b64u text of a picture of 1 to `IMAGE_BYTES` bytes. */
const imageData: Check = (value) => isB64u(value) && value.length > 1 && value.length <= IMAGE_CHARS;

const IMAGE_SHAPE: Readonly<Record<keyof ActionImage, Check>> = {
  type: oneOf('image/jpeg'),
  width: integer(1, IMAGE_WIDTH),
  height: integer(1, 16384),
  data: imageData,
};

const ACTION_SHAPE: Readonly<Record<keyof BrowserAction, Check>> = {
  browser_id: browserId,
  seq: integer(1, Number.MAX_SAFE_INTEGER),
  tool: oneOf(...BROWSER_TOOLS),
  action: (value) => text(64)(value) && value !== '',
  target: nullable(text(1024)),
  text: nullable(text(1024)),
  url: text(8192),
  title: text(1024),
  tab: integer(0, 63),
  ok: flag,
  error: nullable(text(2048)),
  notice: nullable(text(1024)),
  at: time,
  duration_ms: count,
  image: (value) => value === null || matches(value, IMAGE_SHAPE),
  image_omitted: nullable(oneOf(...IMAGE_OMITTED)),
};

const STATE_SHAPE: Readonly<Record<keyof BrowserState, Check>> = {
  browser_id: browserId,
  operation_id: nullable(uuid),
  implicit: flag,
  state: oneOf(...BROWSER_STATES),
  workvm_id: nullable(pattern(/^wvm-[0-9a-f]{8}$/)),
  reason: nullable(oneOf(...BROWSER_REASONS)),
  url: nullable(text(8192)),
  actions: count,
  ready_ms: nullable(count),
  idle_seconds: integer(60, 3600),
  opened_at: nullable(time),
  closed_at: nullable(time),
};

/** A `browser.action` body, or null when it does not follow the contract. */
export const parseBrowserAction = (body: unknown): BrowserAction | null =>
  matches(body, ACTION_SHAPE) ? (body as unknown as BrowserAction) : null;

/** A `browser.state` body, or null when it does not follow the contract. */
export const parseBrowserState = (body: unknown): BrowserState | null =>
  matches(body, STATE_SHAPE) ? (body as unknown as BrowserState) : null;
