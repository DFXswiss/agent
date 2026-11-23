# RUN-10 acceptance evidence (GitHub executors II: checkout, commit, push; guest file import and export)

## Unit and integration tests (`make platform-ci`)
- `tests/platform/runner/github/test_gitobj.py`: blob and tree SHA-1s against values of the git binary - empty file,
  text, binary data, executable, nested trees, the tree sort order (`a-b` before `a/`), deletions down to the empty
  tree, links and submodules as leaf entries, conflicts, recursive tree listings of the Git Data API.
- `tests/platform/runner/github/test_tarsafe.py` (ATK-40 at unit level): symbolic and hard links, character and block
  devices, FIFOs (refused, or skipped and counted for GitHub tarballs), absolute paths, `..`, `//`, `./`, `.git`,
  control characters, GNU long names decoded before the check, duplicates, sparse members, member and byte limits,
  broken, truncated and corrupt archives (also after a PAX header and inside the deflate stream); re-packing without
  the top directory with normalised modes, owners and times.
- `tests/platform/runner/github/test_checkout.py`, `test_commit.py`, `test_push.py`: the RUN-07 pipeline (formal
  checks, pre-checks, dedupe tag, registration at the contract-checking hub, executor retries, result, answer) with
  the GitHub double as a real HTTP server and the real guest file code behind the guest client:
  - checkout: exact REST sequence `GET /repos/{r}`, `GET …/commits/{ref}`, `GET …/tarball/{sha}`,
    `GET /codeload/…` (without token); the resolved SHA as dedupe field (equal tags for the same commit); refs by
    branch (`refs/heads/` form), tag and short SHA; mixed-case repository → lower-case directory; replace on a second
    checkout; workspace content with the symbolic link skipped and modes 0600/0700; `not_found` ref,
    `repo_not_accessible`, `upstream_error` pre-checks; `size_exceeded` (archive and re-packed tar); unsafe archives
    (`..`, absolute → `path_invalid`, garbage → `upstream_error`); guest unreachable (retried 3 times, retryable),
    ended session, odd answer; session without VM; scratch removed; sweep of old scratch directories;
  - commit: exact sequence `GET /repos/{r}`, `GET …/git/ref/heads/{b}`, `GET …/git/commits/{parent}`,
    `GET …/git/trees/{tree}?recursive=1`, `POST …/git/blobs` per changed file, `POST …/git/trees`,
    `POST …/git/commits`; no ref update; parent from the branch or from the session's checkout; GitHub's tree equals the
    local one (the commit's files in the double are compared); executable bit and link kept; `paths`; deletions;
    dedupe tags equal for the same snapshot and different after a change; `tree_mismatch` (double fault, and a blob
    stored under another SHA); a retried `5xx` within one operation keeping the snapshot; `secret_detected` for every
    pattern type (PEM, `ghp_`, `gho_`, `ghu_`, `github_pat_`, `sk-ant-`, `sk-or-`, `AKIA…`) before any write; size
    limits (changes, file, total); `path_invalid`; truncated parent listing; export too large, unreachable or not
    matching its manifest; missing pipeline or VM; revoked token and unusable credential (`github_not_connected`);
    snapshot missing or altered at execution; every way an export can differ from its manifest (17 cases);
  - push: new branch (`POST …/git/refs`), fast-forward (`GET …/compare/{head}...{sha}` then `PATCH` with
    `force:false`), identical branch (no write), backwards and diverged (`non_fast_forward`), a commit of no
    `github.commit` of the task (`not_found`), protected branch; lost races on create and update settled by reading the
    ref; compare `identical`, `behind`, `404`, unknown status, `403`; result URL from `html_url`.
  - Results are validated against `contracts/ops/<type>.result.json`; the double records no failure (forced updates,
    tokens on codeload, unknown routes would be failures).
- `tests/platform/guest/test_files.py` (through the real control socket): import with owner and modes, replace of a
  directory and of a link to the outside (outside untouched), `replace=false`, the malicious archives of ATK-40
  (links, hard links, devices, FIFOs, absolute, `..`, `.git`, control characters, duplicates, file/directory
  conflicts, sparse, broken, truncated), limits including `413` before reading, link and file parents of the target,
  unknown and ended sessions, missing workspace; export with the manifest as first member and `X-Manifest-Sha256`,
  symbolic links and FIFOs skipped, `.git` left out, listed paths (missing, overlapping, through a link), foreign and
  unreadable files, depth limit, size limits, large spooled files, round trip; races called directly (link swapped
  in after `stat`, vanished entries, read errors, a target that keeps coming back, a stream closed early).
- `tests/platform/guest_linux/test_files_linux.py` (`make platform-ci-linux`, root in the `guest-ci` VM): import owned
  by the real session user and group with 0700/0600 and no set-ID bits; links planted by the session never followed by
  import or export (ATK-34 at integration level); export leaves out root-owned files, FIFOs, character devices and
  links; no staging directory left.

Coverage of `agent_runner.github` and of the guest file modules: 100 % lines and branches.

## Gate runs
- `make platform-ci-linux` on the runner device (`guest-ci` VM, root, Landlock ABI 4): 53 passed, including the three
  file tests above.
- `tools/wp/gates.sh RUN-10` (first run): lint, types, contracts and the Linux job green; `platform-test` 8137 passed
  and one hygiene finding in the new tests (literal secret samples), fixed. The final gate run before the merge is
  recorded in the workspace report of the package and repeated by the merge script.
- Review fixes added: header bombs (PAX and GNU long names over 64 KiB), backslash paths, export entry bound, an
  unread export stream, skipped entries kept by `plan`, `trim` of the scratch blobs.

## Not run here
- E2E-21 and E2E-22 do not exist yet (SCN-04); ATK-34 and ATK-40 as attack-suite specs come with ATS-02. The unit and
  integration tests above cover their file and archive cases.
