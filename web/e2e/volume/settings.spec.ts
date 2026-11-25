// Volume gate (ADR-086 §4): the account settings and every settings section, for a member of a large org (the
// security activity of the agents section reads the user's audit).
import type { Locator, Page } from '@playwright/test';
import { test } from './fixtures.ts';
import { measureHere, openView } from './views.ts';

/** Every section of `src/ui/settings/sections/` and the control that shows it loaded. */
const SECTIONS: Readonly<Record<string, (page: Page) => Locator>> = {
  account: (page) => page.getByTestId('settings.account.copy'),
  passkeys: (page) => page.getByTestId('passkeys.add.this-device'),
  billing: (page) => page.getByTestId('settings.billing'),
  agents: (page) => page.locator('[data-testid="settings.agents"]:not(:has([data-testid="agents.loading"]))'),
  credentials: (page) => page.getByTestId('credentials.section'),
  github: (page) => page.getByTestId('github.state'),
  language: (page) => page.getByTestId('settings.language.select'),
  model: (page) => page.getByTestId('model.save'),
  'model-quotas': (page) => page.getByTestId('settings.model-quotas'),
  quotas: (page) => page.getByTestId('quotas.section'),
  subscriptions: (page) => page.getByTestId('settings.subscriptions.link'),
};

export const COVERS = [
  'route:/settings',
  'settings:account',
  'settings:agents',
  'settings:billing',
  'settings:credentials',
  'settings:github',
  'settings:language',
  'settings:model',
  'settings:model-quotas',
  'settings:passkeys',
  'settings:quotas',
  'settings:subscriptions',
  'list:settings.agents.activity',
];

test('route:/settings', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/settings',
    path: '/settings',
    primary: (page) => page.getByTestId('settings.sign-out'),
  });
  // the sections are measured on the desktop only (`PHONE_EXEMPT`, ADR-086 §4)
  if (info.project.name.endsWith('-mobile')) return;
  const page = volume.page;
  for (const [id, control] of Object.entries(SECTIONS)) {
    const section = page.getByTestId(`settings.${id}`);
    const since = Date.now();
    await section.scrollIntoViewIfNeeded({ timeout: 30_000 }).catch(() => undefined);
    await measureHere(volume, info, since, {
      id: `settings:${id}`,
      ready: () => section,
      primary: () => control(page),
      ...(id === 'agents' && { also: ['list:settings.agents.activity'] }),
    });
  }
});
