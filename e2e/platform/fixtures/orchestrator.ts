// Client of the orchestrator's control API (orchestrator/control.py, routes in orchestrator/routes/).
export class OrchestratorError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    detail: string,
  ) {
    super(`${status} ${code}: ${detail}`);
  }
}

export interface RunState {
  readonly topology: string;
  readonly hub: { readonly origin: string; readonly browser_relay: string };
  readonly control: string;
  readonly doubles: Readonly<Record<string, { readonly url: string }>>;
  readonly runners: Readonly<Record<string, string>>;
  readonly links: readonly string[];
}

export interface DoubleState {
  readonly double: string;
  readonly ok: boolean;
  readonly done: boolean;
  readonly failures: readonly string[];
  readonly requests: number;
  readonly [key: string]: unknown;
}

export interface PoolState {
  readonly size: number;
  readonly ready: number;
  readonly golden: string;
  readonly refill: string;
}

export class Orchestrator {
  constructor(
    readonly baseUrl: string,
    private readonly token: string,
    private readonly fetcher: typeof fetch = fetch,
  ) {}

  async call<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
    const response = await this.fetcher(`${this.baseUrl}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.token}` },
      body: body === undefined ? null : JSON.stringify(body),
    });
    const payload = (await response.json()) as { error?: string; detail?: string };
    if (!response.ok) throw new OrchestratorError(response.status, payload.error ?? 'failed', payload.detail ?? '');
    return payload as T;
  }

  state = () => this.call<RunState>('GET', '/state');
  doubleState = (name: string) => this.call<DoubleState>('GET', `/doubles/${name}/state`);
  /**
   * Loads a double's scenario. For the model double it also switches the credential check off (`access: null`): a
   * spec that sets an access kind (`fixtures/model.ts` `loadModelScenario`, E2E-16/-27/-36) must not leave it to the
   * specs after it in the same run, whose sessions use the platform key.
   */
  loadScenario = (name: string, scenario: unknown) =>
    this.call<Record<string, unknown>>(
      'POST',
      `/doubles/${name}/scenario`,
      name === 'model' ? { scenario, access: null } : { scenario },
    );
  resetDouble = (name: string) => this.call<Record<string, unknown>>('POST', `/doubles/${name}/reset`, {});
  doubleRequest = (name: string, method: string, path: string, body?: unknown) =>
    this.call<{ status: number; body: unknown }>('POST', `/doubles/${name}/request`, { method, path, body });
  links = () => this.call<Record<string, { cut: boolean; connections: number }>>('GET', '/links');
  cut = (link: string) => this.call<{ cut: boolean }>('POST', `/links/${link}/cut`);
  restore = (link: string) => this.call<{ cut: boolean }>('POST', `/links/${link}/restore`);
  runners = () => this.call<Record<string, string>>('GET', '/runners');
  stopRunner = (name: string) => this.call<{ state: string }>('POST', `/runners/${name}/stop`);
  /** SIGKILL to the runner's process group, no SIGTERM first (a hard device failure, KOR-58); its VMs stay. */
  killRunner = (name: string) => this.call<{ exit: number; state: string }>('POST', `/runners/${name}/hard-kill`);
  /** Restarts the hub with the device online window and failure deadline (null: the hub's default, 30 and 300 s). */
  setFailover = (online: number | null, failover: number | null) =>
    this.call<{ online_seconds: number | null; failover_seconds: number | null }>('POST', '/hub/failover', {
      online_seconds: online,
      failover_seconds: failover,
    });
  /** Starts a stopped runner; `github` sets its GitHub ways (KOR-65: OAuth App client ID, GitHub App URL name; a
   *  missing member removes the setting). */
  startRunner = (name: string, github?: { oauth_client_id?: string; app_slug?: string }) =>
    this.call<{ state: string }>('POST', `/runners/${name}/start`, github === undefined ? undefined : { github });
  operator = (args: string[]) =>
    this.call<{ rc: number; stdout: string; stderr: string }>('POST', '/operator', { args });
  hubQuery = <T = Record<string, unknown>>(sql: string) =>
    this.call<{ rows: T[] }>('POST', '/hub-db/query', { sql }).then((r) => r.rows);
  advanceClock = (seconds: number) => this.call<unknown>('POST', '/clock/advance', { seconds });
  drainPool = (size = 0) =>
    this.call<Record<string, { deleted: string[]; size: number }>>('POST', '/pool/drain', { size });
  pool = () => this.call<Record<string, PoolState>>('GET', '/pool');
  enroll = (runner: string, code: string, platform: boolean) =>
    this.call<{ rc: number; stdout: string; stderr: string; state: string }>('POST', `/runners/${runner}/enroll`, {
      code,
      platform,
    });
  hooksFault = (status: 500 | null) => this.call<unknown>('POST', '/hooks/fault', { status });
}
