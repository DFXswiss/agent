// Locations, ports and lanes of the Playwright suite. The hub test server listens on WP_PORT_BASE+2 when the work
// package environment is set (ADR-027 section 7), else on WEB_E2E_PORT or 8790; every further lane takes two ports
// above the performance servers (decisions/HOT-21.md).
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export const WEB_DIR = fileURLToPath(new URL('../../', import.meta.url));
export const STATIC_DIR = fileURLToPath(new URL('../../../src/agent_hub/static/', import.meta.url));
export const PROBE_DIR = fileURLToPath(new URL('../.probe/', import.meta.url));

export function e2ePort(env: NodeJS.ProcessEnv = process.env): number {
  if (env.WP_PORT_BASE) return Number(env.WP_PORT_BASE) + 2;
  return Number(env.WEB_E2E_PORT ?? 8790);
}

export const PORT = e2ePort();
export const ORIGIN = `http://localhost:${PORT}`;
/** Static server of the style-injection probe page (same headers as the hub), next to the hub port. */
export const PROBE_PORT = PORT + 1;
export const PROBE_ORIGIN = `http://localhost:${PROBE_PORT}`;
/** The device double of the suite (scripts/hub_test_director.py, decision WEB-04), next to the probe server. */
export const DIRECTOR_PORT = PORT + 2;
/** `e2e/performance.spec.ts` serves its gzip builds on the three ports after the device double. */
const LANE_BASE = 6;

/**
 * Lanes (decisions/HOT-21.md): a lane is a hub test server of its own – own database, device double, community keeper
 * and port pair – so the projects of different lanes run at the same time without seeing each other's devices, users
 * or data. `main` is the hub of every project not listed here.
 */
export const PROJECT_LANES: Readonly<Record<string, string>> = {
  volume: 'volume',
  'volume-mobile': 'volume-mobile',
  'mobile-devices': 'mobile-devices',
  'mobile-iphone-devices': 'mobile-iphone-devices',
  'attention-volume': 'attention-volume',
  devices: 'devices',
};
export const LANES: readonly string[] = ['main', ...new Set(Object.values(PROJECT_LANES))];
/** The lanes of the volume gate: their founder holds the community key (no keeper), their data set is cached. */
export const VOLUME_LANES: readonly string[] = ['volume', 'volume-mobile'];

/** The cache of the volume data set (`scripts/hub_test_volume_cache.py`): WEB_E2E_VOLUME_CACHE, else below
 *  `node_modules/.cache` of the web tree. */
export function volumeCache(env: NodeJS.ProcessEnv = process.env): string {
  return env.WEB_E2E_VOLUME_CACHE || `${WEB_DIR}node_modules/.cache/volume-data`;
}
/** The lane of the worker's project (set by the worker fixture `lane`, `fixtures/test.ts`). */
export const LANE_VARIABLE = 'WEB_E2E_LANE';

export const laneOf = (project: string): string => PROJECT_LANES[project] ?? 'main';

/** The hub port of a lane: `main` on `port`, lane n (1, 2, …) on `port + 6 + 2 (n - 1)`. */
export function lanePort(lane: string, port = PORT): number {
  const index = LANES.indexOf(lane);
  if (index < 0) throw new Error(`unknown lane ${lane}`);
  return index === 0 ? port : port + LANE_BASE + 2 * (index - 1);
}

/** The device double of a lane: next to the probe server for `main`, else the port after the lane's hub. */
export const laneDirectorPort = (lane: string, port = PORT): number =>
  lane === 'main' ? port + 2 : lanePort(lane, port) + 1;

export const laneOrigin = (lane: string): string => `http://localhost:${lanePort(lane)}`;

/** The device double of the current worker's lane (`main` outside a worker of a lane project). */
export function directorOrigin(env: NodeJS.ProcessEnv = process.env): string {
  return `http://127.0.0.1:${laneDirectorPort(env[LANE_VARIABLE] ?? 'main')}`;
}

/** The lanes a run needs: those of the projects named with `--project` (all without one or with a pattern); `main`
 *  always. */
export function lanesOf(argv: readonly string[]): string[] {
  const named: string[] = [];
  argv.forEach((arg, index) => {
    if (arg === '--project' && argv[index + 1]) named.push(argv[index + 1]!);
    else if (arg.startsWith('--project=')) named.push(arg.slice('--project='.length));
  });
  if (!named.length || named.some((project) => project.includes('*'))) return [...LANES];
  return LANES.filter((lane) => lane === 'main' || named.some((project) => laneOf(project) === lane));
}
export const REPO_DIR = fileURLToPath(new URL('../../../', import.meta.url));

/** Python with agent_hub installed: WEB_E2E_PYTHON, else the repository's .venv, else python3 on PATH. */
export function hubPython(env: NodeJS.ProcessEnv = process.env, venv = `${REPO_DIR}.venv/bin/python`): string {
  if (env.WEB_E2E_PYTHON) return env.WEB_E2E_PYTHON;
  return existsSync(venv) ? venv : 'python3';
}
export { TEST_ROOT_KEY } from '../../build/manifest.ts';
