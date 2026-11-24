# HOT-08 decisions (runner crypto copy without a commit stamp)

1. **No commit stamp.** A copied module or test starts with `Copied from <source>.`, the reference path in this
   repository; `ORIGIN.json` keeps per file the sha256 of the reference (and `copy_sha256` of a copied test) and has no
   `commit` and no `repository` field. Since ADR-061 reference and copy live in the same tree and the same commit, so
   the stamp carried no information but changed every copy whenever any copy was refreshed. Undone by
   `copy_tool.normalize`; the content of every copy is unchanged apart from that first line.
2. **One tree, two calls.** `copy_tool.py` reads the reference from the working tree (committed or not; no second
   clone, no `--rev`). `make runner-crypto-copy` writes the copies, `make runner-crypto-copy-check` only reports
   differing files (exit 1). After a change of `src/agent_hub/{crypto,log}`, `src/agent_hub/agents/vectors.py` or
   their tests, call `make runner-crypto-copy` and commit the result with the change.
3. **Deterministic.** The tool imports nothing that reads a commit, a clock, the host or the environment; directory
   listings are sorted and `ORIGIN.json` is written with sorted keys and both lists sorted by `copy` (checked by the
   test of item 5). Writing twice changes nothing; an unchanged reference gives byte-identical copies
   (`test_main_check_and_write`, `test_copies_are_current`). The tool never deletes: after a reference is removed or
   renamed, delete the old copy by hand (`test_origin_lists_every_copied_module`/`…_test` name it).
4. **ORIGIN.json stays pretty-printed (one key per line), not one line per file.** Git's three-way merge treats two
   changed lines as a conflict when no unchanged line separates them. With one line per file, two packages that change
   neighbouring references (e.g. `aead.py` and `agent.py`) would conflict; in the pretty form each entry's `sha256`
   line is separated from the next entry's by the unchanged `source`, `copy` and bracket lines, so changes to
   different references merge cleanly (checked with `git merge-file` and with two probe branches, see the report).
   The pretty form is also what `test_copy.py` already checks in full.
5. **Guard against a return.** `test_nothing_in_the_copy_varies_without_a_reference_change` fails when `ORIGIN.json`
   gets another top-level or entry key, carries a commit hash, a date, a time, a host or `agent-core`, when a first
   line is anything but `Copied from <source>.`, or when the tool imports a module outside a fixed list.
   `test_copies_are_current` replaces the hash-only comparison of the references: it runs the tool on the tree and
   compares every written file byte for byte.
6. `decisions/CRY-03.md` items 2, 3 and 6 and `acceptance/CRY-03.md` described the old stamp and invocation; they
   now point here.
7. **Former call accepted.** The rebase step of the work-package merge tool (outside this repository) calls
   `copy_tool.py --agent-core . --rev HEAD [--check]`. The tool accepts exactly these values (hidden options) and reads
   the working tree, which equals `HEAD` in that clean rebase tree; any other value ends with a usage error, so no
   caller silently gets the tree instead of another commit. The options can go once the merge tool calls
   `copy_tool.py [--check]`.
8. **Untracked files (review, medium, not changed).** The tool reads every `.py` and `.json` file in the reference
   directories, tracked or not, and skips hidden entries and `__pycache__`. Reading the index through `git` would
   bring back a subprocess and the work package asks for an uncommitted reference to be copyable (a new module before
   `git add`). A stray file there is also collected by the hub's own test run, so it is visible in the same place;
   `test_copies_are_current` names it as a differing copy.
