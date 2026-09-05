# A38 job adapters

A38 provides four bounded local-job adapters for repository checks whose lifecycle is
more involved than one command. They are invoked directly as:

```text
agent a38 job ADAPTER --config 'JSON'
```

In an A38 policy, prefer the structured form. The policy loader normalizes it to the
same direct command internally, so the existing report wire format remains unchanged:

```json
{
  "executor": {
    "adapter": "commands",
    "config": {
      "steps": [["python", "-m", "compileall", "src"]]
    }
  }
}
```

`executor` and a handwritten `command` are mutually exclusive. The adapters do not
add policy schema keys of their own and do not read repository-specific runtime files.

## Runtime contract

The runner must export full lowercase 40-character commit identities in
`A38_HEAD_SHA` and `A38_BASE_SHA`. The current working directory may be anywhere in
the target Git worktree; the adapter derives the worktree root with Git and verifies
that `A38_HEAD_SHA` is exactly its current `HEAD` and that `A38_BASE_SHA` resolves to
that exact commit.

Each invocation creates unique work and artifact directories outside the repository.
The artifact path is printed at startup and is retained; scratch work is removed.
Generated image tags and Compose project/container names are unique to the invocation.
There is no ambient root, Node-major, success, or full-run override.

All subprocesses receive argv arrays. Adapter config is never evaluated by a shell or
split into words. JSON rejects duplicate keys and non-finite numbers, and every adapter
rejects unknown keys, wrong nested types, empty/NUL argv entries, unknown placeholders,
and malformed placeholders before npm or Docker can mutate state.

The following placeholders are available in expandable strings:

| Placeholder | Value |
|---|---|
| `{repo}` | Verified target worktree root |
| `{head}` | Verified runner head commit |
| `{base}` | Verified runner base commit |
| `{work}` | Unique external scratch directory |
| `{artifacts}` | Unique retained artifact directory |
| `{project}` | Unique Compose-safe project name |
| `{image:NAME}` | Unique image tag `a38-NAME:RUN_ID` |
| `{companion}` | Verified pristine companion snapshot; Compose only |

In Compose argv, an exact first element `{compose}` expands to `docker compose` plus
the configured project, files, and effective env file. It is invalid anywhere else.

### Common keys

Every adapter accepts these optional keys:

```json
{
  "unset": ["EXAMPLE_TOKEN"],
  "unset_prefixes": ["EXAMPLE_CI_"],
  "env": {
    "OUTPUT_DIR": "{artifacts}/results"
  },
  "lock": "example-shared-check",
  "npm": {
    "node_major": 24,
    "canaries": ["example-package/package.json"]
  },
  "postgres": {
    "image": "postgres:16",
    "user": "postgres",
    "password": "local-test-password",
    "database": "example_test",
    "url_env": "EXAMPLE_TEST_DATABASE_URL",
    "port_env": "EXAMPLE_TEST_DATABASE_PORT"
  }
}
```

`GITHUB_TOKEN`, `GH_TOKEN`, and `NODE_OPTIONS` are always removed first. `unset` and
`unset_prefixes` remove additional inherited values, after which configured `env`
values are deliberately applied. Configured values may use placeholders.

`lock` uses an atomic directory and an owner token. Waiting is finite; an abandoned
lock is never reclaimed automatically. npm installation has a separate per-worktree
lock even when a job also has a shared lock. A cached `node_modules` is reused only
when the package-lock digest, exact Node version, architecture stamp, and every
configured canary match. Canary paths are relative to `node_modules`, not the repo.

Postgres is optional and owned by container ID: the adapter creates the container,
records the returned ID, starts it, obtains a dynamic loopback port, waits for
`pg_isready`, and exports the configured URL and port variables. A failed name conflict
does not confer ownership. Postgres and other owned resources are removed on exit;
cleanup removal failures are warnings and never replace an earlier test failure.

SIGINT and SIGTERM terminate the active subprocess process group, including
descendants, and bound all remaining cleanup to one 25-second deadline. Diagnostics
are skipped on interruption. Normal long-running work has no adapter-imposed timeout;
the enclosing A38 job timeout remains authoritative. If an argv leader exits after
starting background descendants, the adapter terminates that still-owned process group
before returning; inherited output descriptors cannot leave the adapter hung or permit
an orphaned background process to masquerade as success.

## `commands`

`steps` is required and non-empty. A step is either an argv array or an object with
`argv` and an optional `stdout` artifact path. `failure_steps` run only after a main
failure. `advisory_steps` run after success or failure but never change the primary
status. Both diagnostic groups have a hard 15-second timeout per step and are skipped
on interruption. A failure-step or required artifact-write error is hard; advisory
errors are warnings.

```json
{
  "unset_prefixes": ["EXAMPLE_SELECTOR_"],
  "env": {
    "COVERAGE_DIR": "{artifacts}/coverage"
  },
  "npm": {
    "node_major": 24,
    "canaries": ["test-runner/package.json", "typescript/lib/typescript.js"]
  },
  "steps": [
    ["npm", "run", "check"],
    {"argv": ["npm", "run", "test:coverage"]}
  ],
  "failure_steps": [
    {
      "argv": ["npm", "run", "summarize-failure"],
      "stdout": "{artifacts}/failure-summary.md"
    }
  ],
  "advisory_steps": [
    {"argv": ["npm", "run", "dependency-report"]}
  ]
}
```

All commands run in the verified repository root. A main step stops the sequence on
its first nonzero exit and that exit code is preserved.

## `immutable`

`immutable` compares the merge-base selection `BASE...HEAD` beneath `path`. New files
are allowed. Modifications, deletions, renames, copies, and type changes to existing
paths are blocked. When `comment_prefix` is present, a modified existing file is
allowed only when removing all lines whose first non-whitespace text begins with that
prefix makes the base and head blobs byte-identical. There is no implicit comment
pattern when the key is omitted.

```json
{
  "path": "schema/changes",
  "exclude": ["schema/changes/generated"],
  "comment_prefix": "//"
}
```

Paths are literal Git pathspecs and filenames are processed with NUL-delimited Git
output, including spaces, tabs, newlines, colons, and pathspec metacharacters. Missing
blobs and Git errors are failures, never empty-file equivalence.

## `compose`

`compose` builds every declared image from source and runs a test service against a
verified companion repository snapshot. It defaults to the shared `docker-heavy` lock.

```json
{
  "unset_prefixes": ["EXAMPLE_STACK_", "EXAMPLE_REGISTRY_"],
  "env": {
    "EXAMPLE_PROJECT": "{project}",
    "EXAMPLE_APP_REPO": "{repo}",
    "EXAMPLE_APP_IMAGE": "{image:app}",
    "EXAMPLE_COMPANION": "{companion}",
    "EXAMPLE_API_PORT": "0"
  },
  "companion": {
    "directory_env": "EXAMPLE_SERVICES_DIR",
    "ref_env": "EXAMPLE_SERVICES_REF",
    "ref": "main",
    "repository": "example/services"
  },
  "files": ["stack/compose.yml", "stack/compose.tests.yml"],
  "env_file": "stack/.env.generated",
  "up": ["{compose}", "up", "-d", "gateway"],
  "builds": [
    {
      "argv": [
        "docker", "build", "-t", "{image:app}",
        "--build-arg", "GIT_COMMIT={head}", "{repo}"
      ],
      "image": "{image:app}"
    },
    {
      "argv": ["{compose}", "build", "tests"],
      "image": "{image:tests}"
    }
  ],
  "ports": [
    {"service": "gateway", "port": 8080, "env": "EXAMPLE_API_PORT"}
  ],
  "test_service": "tests",
  "test_image": "{image:tests}",
  "artifacts": [
    {"source": "/work/test-results", "destination": "test-results"},
    {"source": "/work/report", "destination": "report"}
  ]
}
```

The directory named by `directory_env` must be the exact root of a clean Git checkout
or worktree. Its `origin` must exactly identify `repository`, and its `HEAD` must equal
the commit resolved by the value of `ref_env`, or by `ref` when that variable is empty.
The adapter uses `git archive`, not the working tree, and safely extracts a pristine
snapshot. Configured files and artifact destinations cannot traverse or escape through
symlinks. The companion source `HEAD` and status are checked again during cleanup.

The effective local Docker endpoint is selected in this order: `DOCKER_CONTEXT`, then
`DOCKER_HOST`, then Docker's active context. Only a local `unix:///` endpoint is
accepted. The isolated Docker config contains links only to regular executable Compose
and Buildx plugins; credentials and `config.json` are not copied.

Each successful build is recorded immediately for owned-only cleanup, including a
partial build sequence. The Compose test container is created without starting,
inspected for its actual ID and attached volume names, and only then started. Test
artifacts are copied before container removal and `compose down` on both test success
and failure. Interruption prioritizes teardown and skips diagnostics/artifact copying.
All Compose calls after an env file exists use the same project/files/env-file prefix.
Teardown failures are advisory; artifact, source-integrity, and lock-ownership failures
cannot turn into a pass. The adapter never prunes Docker and never predeletes names.

## `http-smoke`

`http-smoke` builds one configured image and validates its credential gate and static
artifact surface. It defaults to the shared `docker-heavy` lock.

```json
{
  "dockerfile": "docs/Dockerfile",
  "platform": "linux/amd64",
  "build_args": {"GIT_SHA": "{head}"},
  "container_port": 8080,
  "credentials": {
    "user_env": "EXAMPLE_DOCS_USER",
    "password_env": "EXAMPLE_DOCS_PASSWORD",
    "user": "local-check",
    "password": "local-check-password"
  },
  "health": {"path": "/health", "contains": "ready"},
  "root_path": "/",
  "manifest": {
    "path": "/srv/site/manifest.json",
    "artifacts_key": "artifacts",
    "category_key": "category",
    "path_key": "path",
    "index": "index.html",
    "pdf_category": "documents"
  }
}
```

All fields above are required except `build_args`, whose default is `{}`. The adapter:

1. Builds the configured Dockerfile/platform/build arguments under a unique tag and
   records that tag immediately after build success.
2. Creates a no-credential container, records its returned ID, and requires its
   attached start to exit nonzero.
3. Creates the credentialed container on a Docker-assigned loopback port, records its
   ID, starts it, and polls the health path.
4. Requires the health status/body check, a `401` root request without credentials,
   and a `200` root request with Basic authentication.
5. Copies and strictly validates the configured manifest, requests the index and one
   artifact from every category, and requires an inline `Content-Disposition` when the
   configured PDF category exists.

HTTP paths and response/manifest sizes are bounded. Artifact paths must be safe
relative URL paths. Authenticated redirects are followed only while they remain on the
original loopback scheme, host, and port; credentials are never forwarded to another
origin. Loopback requests explicitly disable ambient HTTP, HTTPS, and all-protocol
proxy settings. Configured credentials are not printed. Container name conflicts never
become ownership, and cleanup addresses only IDs returned by successful creates.

## Prerequisites and limits

- Python 3.11 or newer and Git are required.
- npm lifecycle use requires `node`, `npm`, a root `package-lock.json`, and configured
  canaries that npm installs as regular files.
- Postgres, Compose, and HTTP-smoke lifecycle use requires a local Docker daemon.
- Compose requires the companion checkout and ref selector environment described in
  its config. Private registry login must be performed outside the adapter; credentials
  are not copied into its isolated Docker config.
- Artifacts are local declarations produced by the author run. The adapters strengthen
  repeatability and cleanup but do not make a local-CI report cryptographic proof.
