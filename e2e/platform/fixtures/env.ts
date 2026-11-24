// Environment the orchestrator hands to Playwright (orchestrator/run.py): control API, public hub origin, the
// browser relay that Chromium maps the hub origin to, and the iteration's artefact directory.
export interface E2eEnv {
  readonly controlUrl: string;
  readonly controlToken: string;
  readonly hubOrigin: string;
  readonly browserHub: string;
  readonly iterationDir: string;
  readonly topology: string;
}

const NAMES = {
  controlUrl: 'E2E_CONTROL_URL',
  controlToken: 'E2E_CONTROL_TOKEN',
  hubOrigin: 'E2E_HUB_ORIGIN',
  browserHub: 'E2E_BROWSER_HUB',
  iterationDir: 'E2E_ITERATION_DIR',
  topology: 'E2E_TOPOLOGY',
} as const;

/** The orchestrator's variables; a missing one means the suite was not started through e2e/platform/run.sh. */
export function readEnv(source: NodeJS.ProcessEnv = process.env): E2eEnv {
  const out: Record<string, string> = {};
  for (const [key, name] of Object.entries(NAMES)) {
    const value = source[name];
    if (!value) throw new Error(`${name} is not set: start the suite with e2e/platform/run.sh`);
    out[key] = value;
  }
  return out as unknown as E2eEnv;
}

/**
 * Chromium flags of every run: the hub origin `localhost:<public>` resolves to the browser relay (so the browser
 * link can be cut), and every other destination goes to the closed proxy port 9 of the loopback (hermetic rule,
 * 07-testing.md section 5.2); loopback addresses bypass the proxy.
 */
export function chromiumArgs(env: Pick<E2eEnv, 'hubOrigin' | 'browserHub'>): string[] {
  const hub = new URL(env.hubOrigin);
  return [
    `--host-resolver-rules=MAP ${hub.hostname}:${hub.port} ${env.browserHub}`,
    '--proxy-server=http://127.0.0.1:9',
    '--proxy-bypass-list=localhost;127.0.0.1',
  ];
}
