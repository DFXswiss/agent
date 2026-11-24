# HOT-19 - Reproducible byte code of the runner package

Rules: E-19 (fix the class, not only the instance; the check belongs in the gate).

## Part 1: two builds of one commit gave different `release.json`

Finding: of 10 472 files of two builds of one commit exactly one differed,
`agent_cli/__pycache__/pr_lifecycle.cpython-312.pyc`: the constant `frozenset({"skipped", "neutral"})` was written with
`FLAG_REF` in one build and without it in the other, shifting every later reference number.

Cause (reproduced on a test device and read in the CPython 3.12 sources):

- `marshal` marks an object with `FLAG_REF` when its reference count is above one while it is written, that is when
  the code tree shares it (`Python/marshal.c`, `w_ref`).
- The compiler merges equal constants of one module, so the same `frozenset` used in two functions with different
  constant tuples is one shared object.
- When a code object is created, `intern_string_constants` (`Objects/codeobject.c`) interns the strings of every
  constant `frozenset`; if one of them is replaced by an equal string that is already interned, it builds a new
  `frozenset` for that code object, and the constant is no longer shared.
- The parser interns every identifier as immortal (`Parser/pegen.c`), for the life of the process.

So whether `"skipped"` had been used as a name by a file that the same process compiled earlier decided the bytes.
`compileall -j 0` hands files to worker processes depending on timing and the number of cores; `-j 1` in one fixed
order would be repeatable but would still depend on the order and on the rest of the library. Measured on a test
device: a copy of the stdlib and `agent_cli` compiled with `-j 0` differed from `-j 1` in two files
(`pr_lifecycle`, `coordinator_github`); in one process ten of the 70 files of `agent_cli` flipped `pr_lifecycle`
when compiled before it; the hash seed alone changed nothing. `marshal.loads`/`dumps` does not repair it (the sharing
itself differs).

Decision: every file is compiled by its own fresh interpreter
(`env -i python3 -I -S -B -X utf8 -X pycache_prefix=/dev/null -c ...`),
started by the build in parallel threads. Its state before compiling is fixed: built-in and frozen modules and the
source of the few library modules it imports (it reads no byte code, since nothing exists below `/dev/null`, so it does
not matter which `.pyc` the other processes are writing), no `site`, an empty environment and UTF-8 mode (the
locale would otherwise choose which codec modules the interpreter compiles at start). The compiled bytes are
written exactly as `py_compile` writes an unchecked-hash `.pyc`. The result depends only on the file and the
interpreter, not on cores, order, directory or time.

Rejected: no byte code in the payload (every runner start would compile the library; not measured as needed, since
the per-file way costs seconds); one process with a fixed order (still depends on the rest of the library).

Cost measured on a test device: 1 559 files of a full interpreter library in 6-7 s with 8 cores (before: under 1 s),
about 30 ms of one core per file. For the payload's library on the 10-core build Mac that is an estimate of well
under a minute, not a measurement; the operator's next build records the real time (`board` report of HOT-19).

Gate: `test_byte_code_does_not_depend_on_what_the_compiling_process_compiled_before` compiles a library of 150 files
that use a rare word as a name and one file that shares a `frozenset` of that word between two functions, twice and
alone, the second time under another locale. With the former `compileall -j 0` it failed five times out of five on a
test device; with the correction it passes. A file that does not compile is named in the build log (`pyc_failed`).

## Part 3: the hub answered every HEAD with 404

Cause: routes are registered by `HubRouter` with exactly their method; Starlette adds `HEAD` only to its own `Route`,
not to FastAPI's `APIRoute`, so `match_route` found no route for a `HEAD` and the pipeline answered `404 not_found`.

Decision (one place each, not per route):

- `HubRouter.hub_route` registers `HEAD` with every `GET` route. A `HEAD` therefore passes the whole pipeline as its
  `GET`: authentication class, rights, rate limits, query check and read log (`read.<resource>`). An agent is admitted
  to `HEAD` exactly where it is admitted to the `GET` (`agents.scope.agent_allowed`).
- The pipeline sends the response start of the route, an empty body, and ends the route (`HeadAnswered`; found as
  the exception itself, its `__cause__` or inside an exception group, never through `__context__`, so a failure
  while it unwinds is still logged): a `FileResponse` sends only its start for `HEAD` anyway, so the package file is
  not read, and the browser live channel is not followed. The browser stream is registered before its response
  starts (no event is lost between its opening frames and the first byte) and was released only by its body; a
  response whose body never starts (`HEAD`, and a client gone before the first byte, which already leaked before)
  now releases it too (`stream.routes.OpenedStream`). Review finding (high) of HOT-19. A held (signed) answer is released with an empty body; error answers keep the
  `Content-Length` of their JSON body (`json_response_messages(..., head=True)`). The response signature of a `HEAD`
  covers the bytes sent (none), so a client can verify it.
- The contract index (`contracts/api/INDEX.json`) keeps one row per `GET`; `HEAD` has no row of its own. Tests that
  enumerate the methods of the routes (`core/test_read_log.py`, `core/test_registry.py`, `agents/test_scope.py`)
  skip `HEAD` or expect it next to `GET`; `test_head.py` checks it for every route as discovered.
- A route whose answer changes with every call (`GET /v1/me/audit` lists the read it just recorded) cannot have equal
  lengths in two calls; the test compares its status and other headers and pins this list.

## Part 2: `device add` and `device remove` without a dialog (E-27)

- Way (`ownmac.admin_way`): root caller → `root` (the script runs through `/bin/sh` directly); otherwise
  `sudo -n true` succeeds → `sudo` (the same script and arguments through `sudo -n /bin/sh -c …`, never prompting);
  otherwise the `osascript` dialog as before. The test seam stands in for the dialog and keeps the dialog way. The
  result names the way (`admin`) and returns the sentence for it (`dialog`: the dialog sentence, or "No dialog: …").
  No right is added; the script is the constant of `agent_runner.install.admin`, its arguments as before.
- Owner without a screen session: `owner.resolve` takes the recorded owner, then the spooled one (the owner of the
  request file, recorded by the administrator script), then the user at the screen; on the agent way the spooled owner
  always exists, so nobody at the screen is needed and `owner_unknown` cannot occur there. The runner is a
  LaunchDaemon with `UserName` (`RunAtLoad`, `KeepAlive`), so it starts without a login and after a restart.
- A root caller of `device add` is refused before anything happens (`invalid_argument`, `owner_root`): the request
  file would belong to root, which the administrator script refuses as owner, and root never owns a device; the
  next step tells the agent to run as the account that is to own the device. `device remove` works as root.
- `sudo -n true` is asked once per command; if the cached rights expire before the step, the step fails with
  `install_failed` (`admin_step`/`removal_step`) and the command can be run again.
