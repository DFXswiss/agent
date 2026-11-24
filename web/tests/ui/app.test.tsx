import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it, vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { boot } from '../../src/ui/app/boot';
import { ErrorPage } from '../../src/ui/app/ErrorPage';
import { I18nProvider, useI18n } from '../../src/ui/i18n';
import config from '../../../contracts/fixtures/api/config.response/valid/basic.json';

describe('ErrorPage', () => {
  it('shows the catalog texts and the code in the active locale', () => {
    const onReload = vi.fn();
    render(
      <I18nProvider value={createI18n('de')}>
        <ErrorPage code="platform_root_mismatch" onReload={onReload} />
      </I18nProvider>,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('Nicht vertrauenswürdiger Server');
    expect(screen.getByTestId('error.code')).toHaveTextContent('Fehlercode: platform_root_mismatch');
    fireEvent.click(screen.getByTestId('error.reload'));
    expect(onReload).toHaveBeenCalledOnce();
  });
});

describe('ErrorPage reload', () => {
  it('reloads the page by default', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    render(<ErrorPage code="unavailable" />);
    // jsdom does not navigate (it reports "not implemented"); the browser behaviour is covered by config.spec.ts.
    expect(() => fireEvent.click(screen.getByTestId('error.reload'))).not.toThrow();
  });
});

describe('useI18n', () => {
  it('defaults to English without a provider', () => {
    function Probe() {
      return <p>{useI18n().locale}</p>;
    }
    render(<Probe />);
    expect(screen.getByText('en')).toBeInTheDocument();
  });
});

describe('boot', () => {
  const origin = config.origin;
  const platform = {
    credentials: { create: async () => null, get: async () => null },
    indexedDB: new IDBFactory(),
    storage: null,
    missing: async () => [],
  };

  it('renders the router after a successful configuration check', async () => {
    window.history.replaceState(null, '', '/nowhere');
    const container = document.createElement('div');
    const fetch = vi.fn<typeof globalThis.fetch>(async () => Response.json(config));
    const result = await boot({
      container,
      fetch,
      rootKey: config.platform_root_key,
      origin,
      languages: ['de-CH'],
      ...platform,
    });
    expect(result.ok).toBe(true);
    expect(document.documentElement.lang).toBe('de');
    await waitFor(() => expect(container.textContent).toContain('Seite nicht gefunden'));
  });

  it('renders the blocking error page on a root key mismatch', async () => {
    const container = document.createElement('div');
    const fetch = vi.fn<typeof globalThis.fetch>(async () => Response.json(config));
    const result = await boot({ container, fetch, rootKey: 'A'.repeat(43), origin, languages: [], ...platform });
    expect(result).toEqual({ ok: false, code: 'platform_root_mismatch' });
    expect(document.documentElement.lang).toBe('en');
    await waitFor(() => expect(container.textContent).toContain('Untrusted server'));
  });
});
