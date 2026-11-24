// Locations and the port of the Playwright suite. The hub test server listens on WP_PORT_BASE+2 when the work
// package environment is set (ADR-027 section 7), else on WEB_E2E_PORT or 8790.
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
export const DIRECTOR_ORIGIN = `http://127.0.0.1:${DIRECTOR_PORT}`;
export const REPO_DIR = fileURLToPath(new URL('../../../', import.meta.url));

/** Python with agent_hub installed: WEB_E2E_PYTHON, else the repository's .venv, else python3 on PATH. */
export function hubPython(env: NodeJS.ProcessEnv = process.env, venv = `${REPO_DIR}.venv/bin/python`): string {
  if (env.WEB_E2E_PYTHON) return env.WEB_E2E_PYTHON;
  return existsSync(venv) ? venv : 'python3';
}
export { TEST_ROOT_KEY } from '../../build/manifest.ts';
