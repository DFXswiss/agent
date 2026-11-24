// Registration points of the UI (ADR-027 section 4): each test checks the literal glob, the (still empty) real
// registry and the collector with fixture files discovered by the same glob shape.
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { navEntries } from '../../src/ui/nav';
import { chatItems } from '../../src/ui/registries/chat-items';
import { headerItems } from '../../src/ui/registries/header-items';
import { settingsSections } from '../../src/ui/registries/settings-sections';
import { taskTabs } from '../../src/ui/registries/task-tabs';
import { globPatterns } from '../helpers';

describe('navigation entries (src/ui/screens/<area>/nav.ts)', () => {
  it('uses the specified glob and contains the home entry', () => {
    expect(globPatterns('src/ui/nav.ts')).toEqual(["'./screens/*/nav.ts'"]);
    expect(navEntries().map((entry) => entry.id)).toContain('home');
  });

  it('collects and orders fixture entries', () => {
    const modules = import.meta.glob('../fixtures/registry/screens/*/nav.ts', { eager: true });
    expect(navEntries(modules).map((entry) => entry.id)).toEqual(['demo-a', 'demo-b']);
  });

  it('refuses malformed entries', () => {
    expect(() => navEntries({ './screens/x/nav.ts': { nav: [{ id: 'x' }] } })).toThrow('invalid export "nav"');
    expect(() => navEntries({ './screens/x/nav.ts': { nav: {} } })).toThrow('invalid export "nav"');
  });
});

describe('settings sections (src/ui/settings/sections/<area>/)', () => {
  it('uses the specified glob and discovers the sections of the packages', () => {
    expect(globPatterns('src/ui/registries/settings-sections.ts')).toEqual(["'../settings/sections/*/index.tsx'"]);
    expect(settingsSections().map((section) => section.id)).toContain('language');
  });

  it('collects fixture sections', () => {
    const modules = import.meta.glob('../fixtures/registry/settings/sections/*/index.tsx', { eager: true });
    const [section] = settingsSections(modules);
    render(section ? <section.Component /> : null);
    expect(screen.getByText('demo section')).toBeInTheDocument();
  });

  it('refuses malformed sections', () => {
    expect(() => settingsSections({ './x/index.tsx': { section: { id: 'x', order: 1 } } })).toThrow('section');
  });
});

describe('task tabs (src/ui/task-tabs/<area>/)', () => {
  it('uses the specified glob', () => {
    expect(globPatterns('src/ui/registries/task-tabs.ts')).toEqual(["'../task-tabs/*/index.tsx'"]);
    // WEB-05 adds Chat and Sessions; later packages add further tabs (ADR-027 section 4)
    expect(taskTabs().map((tab) => tab.id)).toEqual(expect.arrayContaining(['chat', 'sessions']));
  });

  it('collects fixture tabs', () => {
    const modules = import.meta.glob('../fixtures/registry/task-tabs/*/index.tsx', { eager: true });
    const [tab] = taskTabs(modules);
    render(tab ? <tab.Component orgId="o" taskId="t1" /> : null);
    expect(screen.getByText('demo tab t1')).toBeInTheDocument();
  });

  it('refuses malformed tabs', () => {
    expect(() => taskTabs({ './x/index.tsx': { tab: { id: 'x', order: 1, labelKey: 'x' } } })).toThrow('tab');
  });
});

describe('chat items (src/ui/chat-items/<kind>.tsx)', () => {
  it('uses the specified glob', () => {
    expect(globPatterns('src/ui/registries/chat-items.ts')).toEqual(["'../chat-items/*.tsx'"]);
    // the base items of WEB-05; operation cards follow in WEB-09
    const kinds = ['prompt', 'session.output', 'session.tool', 'session.file', 'session.hook', 'session.status'];
    expect([...chatItems().keys()]).toEqual(expect.arrayContaining([...kinds, 'countdown']));
  });

  it('collects fixture items keyed by kind', () => {
    const modules = import.meta.glob('../fixtures/registry/chat-items/*.tsx', { eager: true });
    expect([...chatItems(modules).keys()]).toEqual(['session.output']);
  });

  it('requires the kind to match the file name', () => {
    const modules = { './chat-items/prompt.tsx': { chatItem: { kind: 'other', Component: () => null } } };
    expect(() => chatItems(modules)).toThrow('kind "other" must match the file');
    expect(() => chatItems({ './chat-items/prompt.tsx': { chatItem: { kind: 'prompt' } } })).toThrow('chatItem');
  });
});

describe('header items (src/ui/shell/header/<area>.tsx)', () => {
  it('uses the specified glob', () => {
    expect(globPatterns('src/ui/registries/header-items.ts')).toEqual(["'../shell/header/*.tsx'"]);
    expect(headerItems().map((item) => item.id)).toEqual(expect.arrayContaining(['org-switcher', 'credentials']));
  });

  it('collects fixture items', () => {
    const modules = import.meta.glob('../fixtures/registry/shell/header/*.tsx', { eager: true });
    expect(headerItems(modules).map((item) => item.id)).toEqual(['demo']);
  });

  it('refuses malformed items', () => {
    expect(() => headerItems({ './x.tsx': { headerItem: { id: 'x', order: 1 } } })).toThrow('headerItem');
  });
});
