// U-WEB-credentials-dfx-debug (DBG-01, ADR-102 section 2): the settings section "Credentials" lists the DFX debug
// sign-in with its API URL and the number of released workgroups, never the wallet address or the signature.
import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { CredentialsSection } from '../../src/ui/settings/sections/credentials';
import { credWorld, seed } from '../core/credentials/world';
import { mount } from './chat-world';

const BODY = {
  address: 'wallet-address-ui',
  signature: 'signature-secret-ui',
  api_url: 'https://api.example.com/v1',
  use: { wgs: ['01930001-0000-7000-8000-0000000000aa'] },
};

describe('credentials list', () => {
  it('lists the DFX debug sign-in with its API URL and release (ADR-102)', async () => {
    const t = await credWorld();
    t.client.on('GET /v1/me/devices', () => ({ devices: [] }));
    t.client.on('GET /v1/me/credentials/dfx_debug', () => {
      const found = t.hub.records.get('dfx_debug');
      if (found === undefined) throw new HubError('not_found', 404);
      return { record: found.record };
    });
    await seed(t, 'dfx_debug', BODY);
    mount(t, [{ path: '/settings', element: <CredentialsSection /> }], '/settings');
    expect(await screen.findByTestId('credentials.item.dfx_debug.detail')).toHaveTextContent(
      'https://api.example.com/v1, released to 1 workgroup(s)',
    );
    expect(document.body.innerHTML).not.toContain('signature-secret-ui');
    expect(document.body.innerHTML).not.toContain('wallet-address-ui');
  });
});
