// The bandwidth choice of the live picture: three choices, Standard by default and for anything unknown.
import { describe, expect, it } from 'vitest';
import { BANDWIDTH_IDS, bandwidthOf, DEFAULT_KBPS, isBandwidth, kbpsOf } from '../../../src/core/browser/bandwidth';

describe('bandwidth', () => {
  it('offers Economical, Standard and Sharp with their rates and Standard as default', () => {
    expect(BANDWIDTH_IDS).toEqual(['economical', 'standard', 'sharp']);
    expect(BANDWIDTH_IDS.map(kbpsOf)).toEqual([250, 1000, 3000]);
    expect(DEFAULT_KBPS).toBe(1000);
    expect(bandwidthOf(null)).toBe('standard');
    expect(bandwidthOf('sharp')).toBe('sharp');
    expect(bandwidthOf('toString')).toBe('standard');
    expect(isBandwidth(3)).toBe(false);
  });
});
