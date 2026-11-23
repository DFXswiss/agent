// Steps of the M1 scenarios B (SCN-02): test hooks inside the run's VMs (orchestrator/routes/sessions_vm.py, hooks.py),
// the runner's idle interval and store (routes/idle.py, runner_store.py), hub audit rows and the plaintext scan of the
// hub side (routes/plaintext.py, checks/plaintext_scan.py).
import { expect } from '@playwright/test';
import type { Orchestrator } from './orchestrator.ts';
import { uuid } from './platform.ts';

export interface SessionVm {
  readonly vm: string;
  readonly user: string;
  readonly uid: number;
  readonly unit: string;
  readonly main_pid: number;
  /** Command line of the unit's main process (Claude Code after the launcher's exec). */
  readonly command: readonly string[];
}

/** One read attempt as the session user: `error` is the OSError class (`FileNotFoundError`, `PermissionError`). */
export interface Probe {
  readonly ok: boolean;
  readonly error: string | null;
  readonly data: string;
}

export interface FileFact {
  readonly sha256: string | null;
  readonly mode: string;
  readonly owner: string;
}

export interface PlaintextReport {
  readonly findings: readonly { readonly source: string; readonly canary: string; readonly encoding: string }[];
  readonly sources: Readonly<Record<string, number>>;
  readonly sse_frames: number;
}

/** The runners' idle interval before an `idle` main session is suspended (null: the runner's default). */
export const setIdle = (orchestrator: Orchestrator, seconds: number | null) =>
  orchestrator.call<Record<string, { restarted: boolean }>>('POST', '/runners/idle', { seconds });

export const sessionVm = (orchestrator: Orchestrator, session: string) =>
  orchestrator.call<SessionVm>('GET', `/sessions/${uuid(session)}/vm`);

/** VM clock minus the hub device's clock (the browser's), error at most `rtt_ms / 2`. */
export const sessionClock = (orchestrator: Orchestrator, session: string) =>
  orchestrator.call<{ offset_ms: number; rtt_ms: number }>('GET', `/sessions/${uuid(session)}/clock`);

/** The recorder's asciicast copy of the session's terminal. */
export const sessionCast = async (orchestrator: Orchestrator, session: string) =>
  (await orchestrator.call<{ cast: string }>('GET', `/sessions/${uuid(session)}/cast`)).cast;

/** Types `text` straight into the session's tmux pane (test hook; the prompt-origin hook must refuse it). */
export const typeIntoPane = (orchestrator: Orchestrator, session: string, text: string, enter = true) =>
  orchestrator.call<{ vm: string }>('POST', `/sessions/${uuid(session)}/type`, { text, enter });

/** Reads `path` as the session user: inside its unit's mount namespace and in the VM's root namespace. */
export const osRead = (orchestrator: Orchestrator, session: string, path: string) =>
  orchestrator.call<{ vm: string; in_sandbox: Probe; as_user: Probe }>('POST', `/sessions/${uuid(session)}/os-read`, {
    path,
  });

/** Facts (sha256, mode, owner; null = missing) of `paths` in the session's VM or in every running VM of the run. */
export const vmFiles = (orchestrator: Orchestrator, paths: readonly string[], session?: string) =>
  orchestrator.call<Record<string, Record<string, FileFact | null>>>('POST', '/vms/files', {
    paths,
    ...(session !== undefined && { session }),
  });

/** Forces the guest hooks of every running VM of the run to answer HTTP 500 (`500`) or switches them back (`null`). */
export const hooksFault = (orchestrator: Orchestrator, status: 500 | null) =>
  orchestrator.call<{ vms: string[] }>('POST', '/hooks/fault', { status });

/** Rows of a read-only query on a runner's own store. */
export const runnerStore = async <T = Record<string, unknown>>(
  orchestrator: Orchestrator,
  sql: string,
  runner = 'runner-1',
) => (await orchestrator.call<{ rows: T[] }>('POST', `/runners/${runner}/store-query`, { sql })).rows;

export interface AuditRow {
  readonly action: string;
  readonly result: string;
  readonly session_id: string | null;
  readonly task_id: string | null;
  readonly detail: Record<string, unknown>;
}

/** The hub's audit rows of `task` (oldest first), optionally only `action`. */
export async function auditRows(orchestrator: Orchestrator, task: string, action?: string): Promise<AuditRow[]> {
  const only = action === undefined ? '' : ` AND action = '${action.replace(/[^a-z._]/g, '')}'`;
  return orchestrator.hubQuery<AuditRow>(
    `SELECT action, result, session_id, task_id, detail FROM hub.audit WHERE task_id = '${uuid(task)}'${only} ORDER BY seq`,
  );
}

/** The session's state at the hub. */
export async function sessionState(orchestrator: Orchestrator, session: string): Promise<string> {
  const [row] = await orchestrator.hubQuery<{ state: string }>(
    `SELECT state FROM hub.session WHERE id = '${uuid(session)}'`,
  );
  return row?.state ?? 'missing';
}

/**
 * The plaintext scan of the hub side (09-milestones.md M1): none of `canaries` in the hub database dump, the hub logs
 * or the bytes the hub sent to the browser; the browser's live stream must have been captured.
 */
export async function expectNoPlaintext(
  orchestrator: Orchestrator,
  canaries: readonly string[],
): Promise<PlaintextReport> {
  const report = await orchestrator.call<PlaintextReport>('POST', '/checks/plaintext', { canaries });
  expect(report.findings).toEqual([]);
  expect(report.sources['hub-db-dump']).toBeGreaterThan(0);
  expect(report.sources['hub-log']).toBeGreaterThan(0);
  expect(report.sources['postgres-log']).toBeGreaterThan(0);
  expect(report.sse_frames).toBeGreaterThan(0);
  return report;
}
