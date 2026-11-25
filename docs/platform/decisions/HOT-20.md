# HOT-20 - No unsigned Mach-O file in the runner package, not even inside an archive

Rules: E-19 (fix the class, not only the instance), E-25 (no other checks than the one notarisation needs).

## Finding
Notarisation of a signed runner package ended `Invalid` for one file only:
`lima/share/lima/lima-guestagent.Darwin-aarch64.gz/lima-guestagent.Darwin-aarch64`. Apple checks Mach-O files inside
compressed files too; `codesign` signed only loose Mach-O files, found by their magic.

## Left out, not signed
The pinned Lima archive (2.3.0, Darwin arm64) ships two guest agents in `share/lima/`: one for Linux guests and one for
macOS guests. Lima copies the agent matching the guest's operating system into the VM; the host never runs either.
Own devices run Linux guests only (`images/golden.yaml` and `images/workvm.yaml`: no `os:`, so Linux; `vmType: vz`,
`arch: aarch64`), and nothing in the repository names the macOS agent. The build therefore removes
`lima/share/lima/lima-guestagent.Darwin-*` right after extracting Lima. Signing it inside the `.gz` would have meant
unpacking, signing with a time stamp and packing again reproducibly, for a file that is never used.

## The class
A build with `--sign-identity` looks into every archive of the staged payload by content (zip including wheels and
jars, gzip, xz, bzip2, tar, nested) right after staging, before byte code and signing; one archive of the tree
is held in memory at a time. A Mach-O member refuses the build with
`build_failed` `sign_missing`; the log has one `sign_missing path=<archive>/<member>` line per file, and the command
now writes its log lines to standard error when it fails (also `pyc_failed`). An archive whose content does not read
(truncated, encrypted, unsupported method) holds no member for this check. A build without identity is unchanged and unchecked. The manifest and reproducibility
are unaffected: the removal happens before modes, times and the manifest, the same for every build of a commit.
