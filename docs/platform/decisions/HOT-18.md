# HOT-18 – decisions

Package: two faults found while rolling out `0a1207dc`. (1) The build hash of the hub (built in a Linux container)
differed from that of an independent build on macOS. (2) `workvm install` created `lima/_config` of the slot account
as root.

## Part 1 – reproducible web build

1. **Cause: chunk rules on absolute module ids.** HOT-16 stage 2 assigned modules to the chunk groups `start` and
   `screens` with the rule `/[\\/]src[\\/]/` on the module id the bundler hands over. That id is the absolute path. In
   a checkout below a directory named `src`, every library in `node_modules` matched too, so the chunks, their names
   and `index.html` differed. Measured on commit `0a1207dc` with the test root key and the same commit time: macOS
   and the Linux VM in an ordinary directory both gave `5ebdd8c9…`, and macOS below `…/src/agent/web` gave
   `5f75ccca…` (other names: `index-*.js` instead of `start-*.js`). The operating system alone made no difference.
   Before `0a1207dc` the rules (`/src/core/`, `/src/ui/`) did not match library paths, so the hash was stable.
2. **Fix: rules on the path relative to `web/`** (`web/build/chunks.ts`). The groups and their priorities are
   unchanged. In an ordinary checkout the output is byte-identical to before (same `5ebdd8c9…`, same file names), so
   neither the first-load budget nor the time tolerance of HOT-16 moves. A virtual module or one outside `web/` matches
   no group. The React Router global plugin (`build/globals.ts`) also looks at the absolute id, but it only rewrites one
   pinned statement inside React Router, so a parent directory cannot change its output. It stays as it is.
3. **Gate (E-19): the Linux part compares builds.** `make platform-ci-linux` (`scripts/guest-ci.sh`, command `run`;
   alone: `web-build`) now also builds `web/` on the runner device (macOS) and in the `guest-ci` VM. In the VM the
   tree sits below `/var/lib/guest-ci/src/agent/web`, so the directory differs too, with another time zone. Both
   builds get the same `SOURCE_DATE_EPOCH`, `BUILD_COMMIT` and `BUILD_VERSION`; outside a git checkout these are fixed
   values. `web/scripts/build-compare.mjs` compares the two manifests. On any difference it names the files and the
   step is red. The VM gets Node from the work image pins (`images/versions.json`, checked by SHA-256), so both sides
   run the same Node version. `build:check` stays a check on one system: the work-package gates always run the Linux
   part, and GitHub CI has no VM.
4. **Not checked: processor architecture.** Both sides of the gate are arm64. A build on x86-64 is not compared,
   because no test device has one. The bundler's output on the two architectures has not been seen to differ.
