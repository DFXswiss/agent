// Discovery of extension points (ADR-027 section 4): a package adds a file at a registration point and never edits a
// shared list. The callers pass the result of a literal `import.meta.glob(…, { eager: true })`; this module turns it
// into a deterministic list (sorted by module path) and fails loudly on malformed modules.

/** Result of `import.meta.glob(pattern, { eager: true })`: module path → module namespace. */
export type ModuleMap = Readonly<Record<string, unknown>>;

export interface Registered<T> {
  readonly path: string;
  readonly value: T;
}

/** The named export `exportName` of every module, sorted by module path. */
export function collect<T>(
  modules: ModuleMap,
  exportName: string,
  isValid: (value: unknown) => value is T,
): Registered<T>[] {
  return Object.keys(modules)
    .sort()
    .map((path) => {
      const namespace = modules[path];
      const value = isRecord(namespace) ? namespace[exportName] : null;
      if (!isValid(value)) throw new Error(`${path}: missing or invalid export "${exportName}"`);
      return { path, value };
    });
}

export interface Ordered {
  readonly id: string;
  readonly order: number;
}

/** Entries sorted by `order`, then `id`; duplicate IDs are a registration error. */
export function ordered<T extends Ordered>(entries: readonly T[]): T[] {
  const seen = new Set<string>();
  for (const entry of entries) {
    if (seen.has(entry.id)) throw new Error(`duplicate registration "${entry.id}"`);
    seen.add(entry.id);
  }
  return [...entries].sort((a, b) => a.order - b.order || Number(a.id > b.id) - Number(a.id < b.id));
}

/** Last path segment without the given suffix, e.g. `./x/session.output.tsx` → `session.output`. */
export function baseName(path: string, suffix: string): string {
  const name = path.slice(path.lastIndexOf('/') + 1);
  return name.endsWith(suffix) ? name.slice(0, -suffix.length) : name;
}

export const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

export const isNamed = (value: unknown): value is { readonly name: string } =>
  isRecord(value) && typeof value.name === 'string' && value.name !== '';

export type FieldType = 'string' | 'number' | 'component';

const isComponent = (value: unknown): boolean =>
  typeof value === 'function' || (isRecord(value) && '$$typeof' in value);

/** True if `value` is an object whose fields have the given types (`component`: a React component). */
export function hasShape(value: unknown, shape: Readonly<Record<string, FieldType>>): boolean {
  if (!isRecord(value)) return false;
  return Object.entries(shape).every(([field, type]) =>
    type === 'component' ? isComponent(value[field]) : typeof value[field] === type,
  );
}
