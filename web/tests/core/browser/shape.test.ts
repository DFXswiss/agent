// The bodies of `browser.action` and `browser.state` (13-work-vm.md section 13.5): every field present and valid, no
// other field; a masked password stays as stored.
import { describe, expect, it } from 'vitest';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import {
  BROWSER_REASONS,
  BROWSER_STATES,
  IMAGE_BYTES,
  IMAGE_CHARS,
  parseBrowserAction,
  parseBrowserState,
} from '../../../src/core/browser/shape';
import { action, state } from './world';

describe('browser.action', () => {
  it('accepts a valid action, with or without picture, and keeps a masked password as stored', () => {
    expect(parseBrowserAction(action(1))).toEqual(action(1));
    const typed = action(2, { tool: 'browser_type', action: 'type', text: '••••' });
    expect(parseBrowserAction(typed)?.text).toBe('••••');
    expect(parseBrowserAction(action(3, { image: null, image_omitted: 'too_large' }))).not.toBeNull();
    expect(
      parseBrowserAction(action(4, { ok: false, error: 'timeout', notice: 'Dialog accepted', target: null })),
    ).not.toBeNull();
    const largest = b64uEncode(new Uint8Array(IMAGE_BYTES));
    expect(largest.length).toBe(IMAGE_CHARS);
    expect(parseBrowserAction(action(5, { image: { ...action(5).image, data: largest } }))).not.toBeNull();
  });

  it.each([
    ['extra field', { extra: 1 }],
    ['browser id', { browser_id: 'br-XYZ' }],
    ['tool', { tool: 'click' }],
    ['seq', { seq: 0 }],
    ['empty action', { action: '' }],
    ['tab', { tab: 64 }],
    ['ok', { ok: 'yes' }],
    ['time', { at: 'yesterday' }],
    ['text', { text: 'x'.repeat(1025) }],
    ['omitted', { image_omitted: 'lost' }],
    ['image type', { image: { ...action(1).image, type: 'image/png' } }],
    ['image width', { image: { ...action(1).image, width: 961 } }],
    ['image data', { image: { ...action(1).image, data: 'not+b64u' } }],
    ['empty image', { image: { ...action(1).image, data: '' } }],
    ['too large image', { image: { ...action(1).image, data: 'A'.repeat(IMAGE_CHARS + 1) } }],
  ])('refuses a wrong %s', (_name, overrides) => {
    expect(parseBrowserAction(action(1, overrides))).toBeNull();
  });

  it('refuses a missing field', () => {
    const { url: _url, ...rest } = action(1);
    expect(parseBrowserAction(rest)).toBeNull();
    expect(parseBrowserAction(null)).toBeNull();
  });
});

describe('browser.state', () => {
  it('accepts every state and reason, an implicit start without operation and a starting state without VM', () => {
    for (const name of BROWSER_STATES) expect(parseBrowserState(state({ state: name }))).not.toBeNull();
    for (const reason of BROWSER_REASONS) expect(parseBrowserState(state({ state: 'closed', reason }))).not.toBeNull();
    const starting = state({
      state: 'starting',
      implicit: true,
      operation_id: null,
      workvm_id: null,
      url: null,
      ready_ms: null,
      opened_at: null,
    });
    expect(parseBrowserState(starting)).toEqual(starting);
  });

  it.each([
    ['state', { state: 'gone' }],
    ['reason', { reason: 'bored' }],
    ['idle', { idle_seconds: 59 }],
    ['operation', { operation_id: 'op' }],
    ['VM', { workvm_id: 'vm-1' }],
    ['implicit', { implicit: null }],
    ['extra field', { extra: true }],
  ])('refuses a wrong %s', (_name, overrides) => {
    expect(parseBrowserState(state(overrides))).toBeNull();
  });
});
