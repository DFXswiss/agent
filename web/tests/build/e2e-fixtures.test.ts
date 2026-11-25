// The Playwright fixtures that are plain functions: port selection, source path mapping, source maps, the hub test
// server's routing rules and the list of sources the merged coverage gate expects.
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { isAppSource, isBrowserGated, sourcePath } from '../../e2e/fixtures/coverage-options.ts';
import { appSources, withSourceMaps } from '../../e2e/fixtures/coverage.ts';
import {
  directorOrigin,
  e2ePort,
  hubPython,
  LANE_VARIABLE,
  laneDirectorPort,
  laneOf,
  laneOrigin,
  lanePort,
  LANES,
  lanesOf,
  PORT,
  PROJECT_LANES,
  VOLUME_LANES,
  volumeCache,
} from '../../e2e/fixtures/env.ts';
import { CACHE_ASSETS, CACHE_INDEX, SECURITY_HEADERS } from '../../e2e/fixtures/headers.ts';
import { spawn } from 'node:child_process';
import { handler, startHub, testConfig, waitForHealth } from '../../e2e/fixtures/server.ts';
import { WEB_DIR } from '../helpers';

const ORIGIN = 'http://localhost:8790';

function dirs() {
  const base = join(WEB_DIR, 'node_modules', '.tmp');
  mkdirSync(base, { recursive: true });
  const root = mkdtempSync(join(base, 'server-'));
  const staticDir = join(root, 'static');
  const probeDir = join(root, 'probe');
  mkdirSync(join(staticDir, 'assets'), { recursive: true });
  mkdirSync(probeDir);
  writeFileSync(join(staticDir, 'index.html'), '<p>app</p>');
  writeFileSync(join(staticDir, 'assets', 'a.js'), 'js');
  writeFileSync(join(staticDir, 'assets', 'a.js.map'), '{"version":3}');
  writeFileSync(join(probeDir, 'index.html'), '<p>probe</p>');
  return { staticDir, probeDir };
}

function get(path: string) {
  const { staticDir, probeDir } = dirs();
  const serve = handler({ port: 8790, origin: ORIGIN, staticDir, probeDir, rootKey: 'K' });
  const result = { status: 0, headers: {} as Record<string, string>, body: '' };
  const res = {
    writeHead(status: number, headers: Record<string, string>) {
      result.status = status;
      result.headers = headers;
    },
    end(body: string | Buffer) {
      result.body = String(body);
    },
  } as unknown as ServerResponse;
  serve({ url: path } as IncomingMessage, res);
  return result;
}

describe('e2e environment', () => {
  it('uses WP_PORT_BASE+2, else WEB_E2E_PORT, else 8790', () => {
    expect(e2ePort({ WP_PORT_BASE: '20260' })).toBe(20262);
    expect(e2ePort({ WEB_E2E_PORT: '9000' })).toBe(9000);
    expect(e2ePort({})).toBe(8790);
  });

  it('gives every lane its own hub and device double above the performance servers', () => {
    expect(LANES[0]).toBe('main');
    expect(lanePort('main', 100)).toBe(100);
    expect(laneDirectorPort('main', 100)).toBe(102);
    const ports = LANES.slice(1).flatMap((lane) => [lanePort(lane, 100), laneDirectorPort(lane, 100)]);
    expect(ports[0]).toBe(106);
    expect(new Set(ports).size).toBe(ports.length);
    // the work package range ends at WP_PORT_BASE+19 = PORT+17
    expect(Math.max(...ports)).toBeLessThanOrEqual(117);
    expect(() => lanePort('nowhere')).toThrow(/unknown lane/);
    expect(laneOrigin('volume')).toBe(`http://localhost:${lanePort('volume')}`);
    expect(laneOf('volume-mobile')).toBe('volume-mobile');
    expect(laneOf('desktop')).toBe('main');
    expect(directorOrigin({})).toBe(`http://127.0.0.1:${PORT + 2}`);
    expect(directorOrigin({ [LANE_VARIABLE]: 'volume' })).toBe(`http://127.0.0.1:${lanePort('volume') + 1}`);
  });

  it('keeps the volume data set in WEB_E2E_VOLUME_CACHE, else below node_modules', () => {
    expect(volumeCache({ WEB_E2E_VOLUME_CACHE: '/cache' })).toBe('/cache');
    expect(volumeCache({})).toBe(join(WEB_DIR, 'node_modules', '.cache', 'volume-data'));
    expect(VOLUME_LANES.every((lane) => LANES.includes(lane))).toBe(true);
  });

  it('names only projects of the Playwright configuration as lanes', async () => {
    const { default: config } = await import('../../playwright.config.ts');
    const names = (config.projects ?? []).map((project) => project.name);
    for (const project of Object.keys(PROJECT_LANES)) expect(names).toContain(project);
    // every project with the device double that is not in the main lane runs one test at a time
    for (const project of config.projects ?? [])
      if (laneOf(project.name!) !== 'main') expect(project.workers, project.name).toBe(1);
  });

  it('starts the lanes of the selected projects only', () => {
    expect(lanesOf([])).toEqual(LANES);
    expect(lanesOf(['test', '--project', 'desktop'])).toEqual(['main']);
    expect(lanesOf(['--project', 'volume*'])).toEqual(LANES);
    expect(lanesOf(['--project=volume', '--project', 'volume-mobile', '--project'])).toEqual([
      'main',
      'volume',
      'volume-mobile',
    ]);
    expect(lanesOf(['--project', 'Volume', 'devices', '--workers', '2', 'e2e/x.spec.ts'])).toEqual([
      'main',
      'volume',
      'devices',
    ]);
  });

  it('picks the Python of the hub', () => {
    expect(hubPython({ WEB_E2E_PYTHON: '/opt/py' })).toBe('/opt/py');
    expect(hubPython({}, join(WEB_DIR, 'package.json'))).toBe(join(WEB_DIR, 'package.json'));
    expect(hubPython({}, join(WEB_DIR, 'missing'))).toBe('python3');
  });

  it('stops waiting when the hub process exits or never answers', async () => {
    const quick = spawn(process.execPath, ['-e', 'process.exit(3)']);
    await new Promise((resolve) => quick.once('exit', resolve));
    await expect(waitForHealth('http://127.0.0.1:9/v1/health', quick)).rejects.toThrow('exited with 3');
    const slow = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 5000)']);
    await expect(waitForHealth('http://127.0.0.1:9/v1/health', slow, 300)).rejects.toThrow('not healthy');
    slow.kill();
    await expect(
      startHub({ python: process.execPath, script: '-e', port: 9, rootKey: 'process.exit(4)' }),
    ).rejects.toThrow();
  });

  it('maps every spelling of an app source', () => {
    expect(sourcePath('../../../../web/src/main.tsx')).toBe('src/main.tsx');
    expect(sourcePath('localhost-1/assets/src/ui/x.tsx')).toBe('src/ui/x.tsx');
    expect(sourcePath('src\\core\\a.ts')).toBe('src/core/a.ts');
    expect(sourcePath('node_modules/react/index.js')).toBe('node_modules/react/index.js');
    expect(isAppSource('src/a.ts')).toBe(true);
    expect(isAppSource('src/vite-env.d.ts')).toBe(false);
    expect(isAppSource('src/a.json')).toBe(false);
    // the core is gated by Vitest alone; the browser data covers the rest of src/
    expect(isBrowserGated('src/ui/a.tsx')).toBe(true);
    expect(isBrowserGated('src/core/a.ts')).toBe(false);
    expect(isBrowserGated('src/a.json')).toBe(false);
  });

  it('lists the app sources the gate expects', () => {
    expect(appSources()).toEqual(expect.arrayContaining(['src/main.tsx', 'src/core/registry.ts']));
    expect(appSources()).not.toContain('src/vite-env.d.ts');
  });

  it('attaches hidden source maps to bundle entries only', () => {
    const { staticDir } = dirs();
    const entries = withSourceMaps(
      [{ url: `${ORIGIN}/assets/a.js` }, { url: `${ORIGIN}/assets/b.js` }, { url: `${ORIGIN}/probe/p.js` }],
      staticDir,
    );
    expect(entries).toEqual([
      { url: `${ORIGIN}/assets/a.js`, sourceMap: { version: 3 } },
      { url: `${ORIGIN}/assets/b.js` },
      { url: `${ORIGIN}/probe/p.js` },
    ]);
  });
});

describe('hub test server', () => {
  it('serves the configuration for its origin', () => {
    const response = get('/v1/config');
    expect(response.headers).toMatchObject({ ...SECURITY_HEADERS, 'Content-Type': 'application/json' });
    expect(JSON.parse(response.body)).toEqual(testConfig(ORIGIN, 'K'));
    expect(testConfig(ORIGIN, 'K').rp_id).toBe('localhost');
  });

  it.each([
    ['/v1/health', 200, '{"ok":true}', CACHE_INDEX],
    ['/v1/other', 404, '{"error":"not_found"}', CACHE_INDEX],
    ['/', 200, '<p>app</p>', CACHE_INDEX],
    ['/o/x/t/y', 200, '<p>app</p>', CACHE_INDEX],
    ['/assets/a.js', 200, 'js', CACHE_ASSETS],
    ['/assets/missing.js', 404, 'not found', CACHE_INDEX],
    ['/assets/../../index.html', 200, '<p>app</p>', CACHE_INDEX],
    ['/probe/', 200, '<p>probe</p>', CACHE_INDEX],
  ])('%s → %i', (path, status, body, cache) => {
    const response = get(path);
    expect([response.status, response.body, response.headers['Cache-Control']]).toEqual([status, body, cache]);
    expect(response.headers['Content-Security-Policy']).toBe(SECURITY_HEADERS['Content-Security-Policy']);
  });
});
