// Test servers of the Playwright suite (docs/platform/decisions/WEB-01.md):
// - `startHub`: the real hub (`python -m agent_hub` via scripts/hub_test_server.py on its own database) serving the
//   build from src/agent_hub/static/ – the server of every app spec;
// - `startHubTestServer`: a Vite preview server with the hub's headers (CSP and security headers on every response,
//   `no-cache` index, `immutable` assets, SPA fallback, `/v1/config`, `/v1/health`, 404 for unknown `/v1/` and
//   `/assets/` paths) that serves the style-injection probe page under /probe/, which the hub does not serve.
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { extname, join, normalize } from 'node:path';
import { preview, type PreviewServer } from 'vite';
import { CACHE_ASSETS, CACHE_INDEX, SECURITY_HEADERS } from './headers.ts';

export interface HubTestServerOptions {
  readonly port: number;
  readonly origin: string;
  readonly staticDir: string;
  readonly probeDir: string;
  readonly rootKey: string;
}

const TYPES: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.map': 'application/json',
  '.woff2': 'font/woff2',
  '.svg': 'image/svg+xml',
};

export function testConfig(origin: string, rootKey: string) {
  return {
    rp_id: new URL(origin).hostname,
    origin,
    platform_root_key: rootKey,
    hub_key: { id: 'hub-test', public_key: 'KSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSk' },
    version: '0.1.0',
    limits: {},
    models: { platform: ['deepseek/deepseek-v4.1-flash-1017'], api_key: [], subscription: [] },
    defaults: { platform: 'deepseek/deepseek-v4.1-flash-1017' },
  };
}

function send(res: ServerResponse, status: number, type: string, body: string | Buffer, cache = CACHE_INDEX) {
  res.writeHead(status, { ...SECURITY_HEADERS, 'Content-Type': type, 'Cache-Control': cache });
  res.end(body);
}

/** Resolves `path` below `dir`; null for traversal or missing files. */
function fileIn(dir: string, path: string): string | null {
  const full = normalize(join(dir, path));
  return full.startsWith(normalize(dir)) && existsSync(full) && !full.endsWith('/') ? full : null;
}

export function handler(options: HubTestServerOptions) {
  const config = JSON.stringify(testConfig(options.origin, options.rootKey));
  return (req: IncomingMessage, res: ServerResponse) => {
    const path = new URL(req.url ?? '/', options.origin).pathname;
    if (path === '/v1/config') return send(res, 200, 'application/json', config);
    if (path === '/v1/health') return send(res, 200, 'application/json', '{"ok":true}');
    if (path.startsWith('/v1/')) return send(res, 404, 'application/json', '{"error":"not_found"}');
    const [dir, rel, cache] = path.startsWith('/probe/')
      ? [options.probeDir, path.slice('/probe'.length), CACHE_INDEX]
      : [options.staticDir, path, path.startsWith('/assets/') ? CACHE_ASSETS : CACHE_INDEX];
    const file = fileIn(dir, rel === '/' ? '/index.html' : rel);
    if (file) return send(res, 200, TYPES[extname(file)] ?? 'application/octet-stream', readFileSync(file), cache);
    if (path.startsWith('/assets/')) return send(res, 404, 'text/plain', 'not found');
    return send(res, 200, TYPES['.html']!, readFileSync(join(dir, 'index.html')));
  };
}

export async function startHubTestServer(options: HubTestServerOptions): Promise<PreviewServer> {
  return preview({
    configFile: false,
    logLevel: 'warn',
    build: { outDir: options.staticDir },
    preview: { host: 'localhost', port: options.port, strictPort: true, open: false },
    plugins: [
      {
        name: 'hub-test-server',
        configurePreviewServer(server) {
          // Registered before Vite's static and SPA middlewares, so every response comes from the hub rules above.
          server.middlewares.use(handler(options));
        },
      },
    ],
  });
}

export interface HubProcess {
  readonly process: ChildProcess;
  stop(): Promise<void>;
}

/** Polls `url` until it answers 200 or the process exits; throws after `timeoutMs`. */
export async function waitForHealth(url: string, child: ChildProcess, timeoutMs = 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`hub test server exited with ${child.exitCode}`);
    const ok = await fetch(url).then(
      (response) => response.ok,
      () => false,
    );
    if (ok) return;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`hub test server not healthy after ${timeoutMs} ms`);
}

/** Starts the real hub on `port`; `stop()` ends it and lets the launcher drop its database. */
export async function startHub(options: {
  readonly python: string;
  readonly script: string;
  readonly port: number;
  readonly rootKey: string;
  /** Also start the device double on this port (decision WEB-04). */
  readonly directorPort?: number;
}): Promise<HubProcess> {
  const args = [options.script, '--port', String(options.port), '--root-key', options.rootKey];
  if (options.directorPort !== undefined) args.push('--director-port', String(options.directorPort));
  const child = spawn(options.python, args, { stdio: ['ignore', 'inherit', 'inherit'] });
  const exited = new Promise<void>((resolve) => child.once('exit', () => resolve()));
  const stop = async () => {
    if (child.exitCode === null) child.kill('SIGTERM');
    await exited;
  };
  try {
    await waitForHealth(`http://localhost:${options.port}/v1/health`, child);
    if (options.directorPort !== undefined)
      await waitForHealth(`http://127.0.0.1:${options.directorPort}/health`, child);
  } catch (error) {
    await stop();
    throw error;
  }
  return { process: child, stop };
}
