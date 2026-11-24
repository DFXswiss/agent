// Chunk groups of the production build (06-web.md sections 10.3 and 12.2, decision HOT-18): a module is assigned by
// its path relative to web/, so the same commit gives the same chunks in any checkout directory – also one below a
// directory named `src`, where a rule on the absolute id put the libraries into the app chunks.
import { describe, expect, it } from 'vitest';
import { chunkGroups, groupOf } from '../../build/chunks.ts';

const WEBS = ['/home/u/agent/web', '/src/agent/web', '/usr/src/app/web', '/x/node_modules/y/web'];

const at = (web: string, path: string): string => `${web}/${path}`;

describe('chunk groups', () => {
  it.each(WEBS)('assigns modules by their path relative to web/ (checkout %s)', (web) => {
    const group = (path: string) => groupOf(chunkGroups(web), at(web, path));
    expect(group('node_modules/react-dom/client.js')).toBe('framework');
    expect(group('node_modules/react-router-dom/dist/index.js')).toBe('framework');
    expect(group('node_modules/@remix-run/router/dist/router.js')).toBe('framework');
    expect(group('node_modules/x/node_modules/scheduler/index.js')).toBe('framework');
    expect(group('src/main.tsx')).toBe('start');
    expect(group('src/ui/screens/Settings.tsx')).toBe('start');
    expect(group('node_modules/micromark/dev/lib/src/x.js')).toBeUndefined();
    expect(group('node_modules/@xterm/xterm/src/browser/Terminal.ts')).toBeUndefined();
    expect(group('node_modules/react-markdown/lib/index.js')).toBeUndefined();
  });

  it('keeps the heavy lazy parts out of the screens chunk', () => {
    const web = '/src/agent/web';
    const screens = chunkGroups(web).find((group) => group.name === 'screens')!;
    expect(screens.test(`${web}/src/ui/screens/Settings.tsx`)).toBe(true);
    expect(screens.test(`${web}/src/ui/task-tabs/chat/Markdown.tsx`)).toBe(false);
    expect(screens.test(`${web}/src/ui/charts/UPlotChart.tsx`)).toBe(false);
    expect(screens.test(`${web}/src/ui/terminal/TerminalView.tsx`)).toBe(false);
    expect(screens.test(`${web}/node_modules/uplot/dist/uPlot.esm.js`)).toBe(false);
  });

  it('only the start group is limited to the first load', () => {
    expect(chunkGroups('/w').map(({ name, tags, priority }) => ({ name, tags, priority }))).toEqual([
      { name: 'framework', tags: undefined, priority: 30 },
      { name: 'start', tags: ['$initial'], priority: 20 },
      { name: 'screens', tags: undefined, priority: 10 },
    ]);
  });

  it('matches no virtual module of the bundler', () => {
    const groups = chunkGroups('/src/agent/web');
    expect(groupOf(groups, '\0vite/preload-helper.js')).toBeUndefined();
    expect(groupOf(groups, 'src/main.tsx')).toBeUndefined();
  });
});
