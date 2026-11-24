// TypeScript-side harness of the contracts (docs/platform/spec/07-testing.md section 4, CON-01).
//
// `node contracts/tools/validate.mjs` (after `npm ci` in contracts/) compiles every schema with Ajv in strict mode,
// refuses keywords outside the whitelist of contracts/README.md and classifies every fixture
// (fixtures/<schema path without .json>/{valid,invalid}/*.json). Exit status 0 only if everything holds.
// The web tests import `loadContracts()` to validate the messages of the web client (WEB-01).

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import Ajv2020 from "ajv/dist/2020.js";

export const CONTRACTS = join(dirname(fileURLToPath(import.meta.url)), "..");
export const ID_BASE = "https://agent.dfx.swiss/contracts/";
export const ALLOWED_KEYWORDS = new Set([
  "type",
  "properties",
  "required",
  "additionalProperties",
  "enum",
  "const",
  "pattern",
  "minLength",
  "maxLength",
  "minimum",
  "maximum",
  "items",
  "minItems",
  "maxItems",
  "minProperties",
  "maxProperties",
  "propertyNames",
  "$ref",
  "$defs",
  "oneOf",
  "anyOf",
  "$schema",
  "$id",
  "title",
  "description",
  "$comment",
]);
const SKIPPED_DIRS = new Set(["node_modules", "fixtures", "__pycache__", ".pytest_cache"]);

function walk(dir, out = []) {
  for (const name of readdirSync(dir).sort()) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      if (!SKIPPED_DIRS.has(name)) walk(path, out);
    } else if (name.endsWith(".json")) {
      out.push(path);
    }
  }
  return out;
}

function posix(path) {
  return path.split(sep).join("/");
}

/** Pointers of keywords outside the whitelist, walking only schema positions (enum/const values are data). */
export function keywordViolations(schema, pointer = "") {
  if (schema === null || typeof schema !== "object" || Array.isArray(schema)) return [];
  const found = [];
  for (const key of Object.keys(schema)) {
    if (!ALLOWED_KEYWORDS.has(key)) found.push(`${pointer}/${key}`);
  }
  for (const key of ["properties", "$defs"]) {
    for (const [name, child] of Object.entries(schema[key] ?? {})) {
      found.push(...keywordViolations(child, `${pointer}/${key}/${name}`));
    }
  }
  for (const key of ["items", "additionalProperties", "propertyNames"]) {
    found.push(...keywordViolations(schema[key], `${pointer}/${key}`));
  }
  for (const key of ["oneOf", "anyOf"]) {
    (schema[key] ?? []).forEach((child, index) => found.push(...keywordViolations(child, `${pointer}/${key}/${index}`)));
  }
  return found;
}

/**
 * All schemas below `root` (files whose top level declares `$schema`) in one strict Ajv instance. A schema that Ajv
 * refuses throws, unless `tolerant` is set: then it is left out and its message is kept in `failures`.
 */
export function loadContracts(root = CONTRACTS, { tolerant = false } = {}) {
  const schemas = new Map();
  for (const path of walk(root)) {
    const document = JSON.parse(readFileSync(path, "utf8"));
    if (document !== null && typeof document === "object" && !Array.isArray(document) && "$schema" in document) {
      schemas.set(posix(relative(root, path)), document);
    }
  }
  const ajv = new Ajv2020({ strict: true, allErrors: true, validateFormats: false });
  const failures = new Map();
  for (const [rel, schema] of schemas) {
    try {
      ajv.addSchema(schema);
    } catch (error) {
      if (!tolerant) throw new Error(`${rel}: ${error.message}`);
      failures.set(rel, error.message);
    }
  }
  return {
    root,
    schemas,
    failures,
    ajv,
    validator(rel) {
      const validate = ajv.getSchema(ID_BASE + rel);
      if (!validate) throw new Error(`unknown schema ${rel}`);
      return validate;
    },
    validate(rel, instance) {
      const validate = this.validator(rel);
      return validate(instance) ? [] : validate.errors;
    },
  };
}

function isLibrary(schema) {
  return !Object.keys(schema).some((key) => ALLOWED_KEYWORDS.has(key) && !["$defs", "$schema", "$id", "title", "description", "$comment"].includes(key));
}

function fixtureFiles(root, rel, kind) {
  const dir = join(root, "fixtures", rel.replace(/\.json$/, ""), kind);
  try {
    return readdirSync(dir)
      .filter((name) => name.endsWith(".json"))
      .sort()
      .map((name) => join(dir, name));
  } catch {
    return [];
  }
}

/** Run every check; returns the list of problems (empty = green) and counters. */
export function check(root = CONTRACTS) {
  const problems = [];
  let fixtures = 0;
  let contracts;
  try {
    contracts = loadContracts(root, { tolerant: true });
  } catch (error) {
    return { problems: [`loading: ${error.message}`], schemas: 0, fixtures };
  }
  for (const [rel, schema] of contracts.schemas) {
    if (contracts.failures.has(rel)) {
      problems.push(`${rel}: ${contracts.failures.get(rel)}`);
      continue;
    }
    if (schema.$id !== ID_BASE + rel) problems.push(`${rel}: $id ${schema.$id}`);
    for (const pointer of keywordViolations(schema)) problems.push(`${rel}: keyword ${pointer} not allowed`);
    try {
      contracts.validator(rel);
    } catch (error) {
      problems.push(`${rel}: ${error.message}`);
      continue;
    }
    if (isLibrary(schema)) continue;
    for (const kind of ["valid", "invalid"]) {
      const files = fixtureFiles(root, rel, kind);
      if (files.length === 0) problems.push(`${rel}: no ${kind} fixture`);
      for (const file of files) {
        fixtures += 1;
        const errors = contracts.validate(rel, JSON.parse(readFileSync(file, "utf8")));
        if (kind === "valid" && errors.length > 0) {
          problems.push(`${posix(relative(root, file))}: ${JSON.stringify(errors[0])}`);
        }
        if (kind === "invalid" && errors.length === 0) problems.push(`${posix(relative(root, file))}: accepted`);
      }
    }
  }
  return { problems, schemas: contracts.schemas.size, fixtures };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { problems, schemas, fixtures } = check(process.argv[2] ?? CONTRACTS);
  for (const problem of problems) console.error(problem);
  console.log(`contracts: ${schemas} schemas, ${fixtures} fixtures, ${problems.length} problems`);
  process.exitCode = problems.length === 0 ? 0 : 1;
}
