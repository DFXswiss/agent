// Build manifest (06-web.md section 10.3): `build-manifest.json` = {version, commit, files:[{path, sha256, size}],
// root_key_fingerprint}; its SHA-256 is the build hash. Also keeps `.keep` in the output directory, which Vite
// empties before every build.
import { createHash } from 'node:crypto';
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import type { Plugin } from 'vite';

export const MANIFEST_FILE = 'build-manifest.json';

/**
 * TEST platform root key: Ed25519 public key of the seed SHA-256("agent.dfx.swiss test platform root key")
 * (docs/platform/decisions/WEB-01.md). Used when VITE_PLATFORM_ROOT_KEY is not set; a release build refuses it.
 */
export const TEST_ROOT_KEY = '4es19ejjw83HvBQaTcmCqzLKLe7OQffXqDUsvsMeYkE';

/** The key to compile in: VITE_PLATFORM_ROOT_KEY, else the test key. */
export function compiledRootKey(env: Readonly<Record<string, string | undefined>> = process.env): string {
  return env.VITE_PLATFORM_ROOT_KEY || TEST_ROOT_KEY;
}
export const KEEP_FILE = '.keep';

export interface ManifestFile {
  readonly path: string;
  readonly sha256: string;
  readonly size: number;
}

export interface Manifest {
  readonly version: string;
  readonly commit: string;
  readonly files: ManifestFile[];
  readonly root_key_fingerprint: string;
}

export interface ManifestInput {
  readonly version: string;
  readonly commit: string;
  readonly rootKey: string;
}

export const sha256Hex = (bytes: Uint8Array | string): string => createHash('sha256').update(bytes).digest('hex');

/** Canonical unpadded base64url → bytes; throws on any other spelling. */
export function decodeB64u(text: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]*$/.test(text) || text.length % 4 === 1) throw new Error('not canonical base64url');
  const bytes = Buffer.from(text, 'base64url');
  if (bytes.toString('base64url') !== text) throw new Error('not canonical base64url');
  return new Uint8Array(bytes);
}

/** SHA-256 (hex) of the 32-byte Ed25519 platform root public key. */
export function rootKeyFingerprint(rootKey: string): string {
  let bytes: Uint8Array;
  try {
    bytes = decodeB64u(rootKey);
  } catch {
    bytes = new Uint8Array();
  }
  if (bytes.length !== 32) throw new Error('VITE_PLATFORM_ROOT_KEY must be a b64u Ed25519 public key (32 bytes)');
  return sha256Hex(bytes);
}

/** Every file below `dir` except `.keep` and the manifest, sorted by POSIX path. */
export function listFiles(dir: string): ManifestFile[] {
  const files: ManifestFile[] = [];
  for (const entry of readdirSync(dir, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile()) continue;
    const full = join(entry.parentPath, entry.name);
    const path = relative(dir, full).split(sep).join('/');
    if (path === KEEP_FILE || path === MANIFEST_FILE) continue;
    const bytes = readFileSync(full);
    files.push({ path, sha256: sha256Hex(bytes), size: bytes.length });
  }
  return files.sort((a, b) => (a.path < b.path ? -1 : 1));
}

export function renderManifest(manifest: Manifest): string {
  return `${JSON.stringify(manifest, null, 2)}\n`;
}

export function writeManifest(dir: string, input: ManifestInput): string {
  const text = renderManifest({
    version: input.version,
    commit: input.commit,
    files: listFiles(dir),
    root_key_fingerprint: rootKeyFingerprint(input.rootKey),
  });
  writeFileSync(join(dir, MANIFEST_FILE), text);
  return sha256Hex(text);
}

/** Vite plugin: emits `.keep` and writes the manifest after all other output (including SRI) is on disk. */
export function buildManifest(input: ManifestInput): Plugin {
  return {
    name: 'agent-platform-build-manifest',
    apply: 'build',
    enforce: 'post',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: KEEP_FILE, source: '' });
    },
    writeBundle: {
      sequential: true,
      order: 'post',
      handler(options) {
        writeManifest(options.dir!, input);
      },
    },
  };
}
