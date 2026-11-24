# HOT-17 – decisions

Package: urgent fix of the work image export. With slot accounts, `agent-runner workvm image build` failed at the
`export` step with `runner.error code=internal name=FileNotFoundError`.

## Finding

`image.Builder` exported `<instance dir>/diffdisk`. Lima 2.x has no `basedisk` and `diffdisk`. With Lima 2.3.0 an
instance directory holds one file `disk`. For `vz` it is a raw GPT disk at the template's full size, written sparse.
The hermetic suites never caught this: their `limactl` stand-in wrote `diffdisk`. Before this package, no export had
ever run against real Lima.

Checked against real Lima 2.3.0 on a test device, with every assumption the work VM code makes about the instance
directory:

- `limactl create` from the work template writes `disk` straight away and resizes it to the template's `disk`. The
  start and the stop leave the layout unchanged.
- The base of a slot comes from `create` with the base template (`images: location` set to the exported `.img`, no
  digest). `create` copies the local raw image into the base's own `disk`. This takes under a second on APFS, and the
  base is never started.
- `limactl clone --set .disk=<larger>` of that base and then `start` grow the disk and the root file system. The marker
  file written while the image was being built is present in the clone.
- A stopped instance still has `lima.yaml`, which `Lima._remove_partial` relies on. Sizes and the disk quota are taken
  from the store and do not depend on files in the instance directory.

## Decisions

1. **One place names the disk file.** `shell.disk_path` takes the first of `DISK_NAMES = ("disk", "diffdisk")` that
   exists. `disk` is the Lima 2.x name. `diffdisk` keeps instances of older Lima working, at no cost. When neither
   exists, the function raises `FileNotFoundError` with the path `<dir>/disk`.
2. **A failure carries its location (goal 3).** When a failed build step raised an `OSError` that names a file, the
   failure line carries that path: `workvm.image.failed step=<step> name=<instance> path=<path>`. An `OSError` in a
   build step now raises `workvm_image_failed <step>` and is no longer reported as `internal`. When a slot's base is
   created at the start of a VM and the exported disk is missing, the start ends with `workvm_image_missing <image>`
   before Lima is asked. The run log then holds `runner.workvm.base.failed slot=<n> path=<disk>`. The manager already
   reports both as `start_failed`/`pool_failed code=…`.
3. **`Bases` takes the directory of the exported disks** (`images`, default `IMAGES_DIR`). `None` means self mode and
   replaces the earlier check on the slot accounts. The two are equivalent in production. With the directory as a
   parameter, the real test can let the test account play the slot without root.
4. **The real test runs in the gates, not under `-m vm`.** `tests/platform/runner/workvm/test_real_lima.py` follows
   `test_lima_accepts_the_template`: it runs wherever real `limactl` 2.3.0 or later is installed on macOS, which
   covers the test devices in `platform-test`, and it is skipped with a reason elsewhere (CI runners, Linux). A
   missing `AGENT_RUNNER_VM_PREFIX` fails the test. The `vm` marker of the session suites would keep it out of
   `make platform-ci`, and the package asks for the opposite: a new Lima version has to show up in the gate.
5. **The test uses a small work template and a stand-in provisioning script.** The template is `images/workvm.yaml`
   with its pinned image and every Lima setting, at 2 CPUs, 2 GiB and 10 GiB. The provisioning script installs the
   three commands of the smoke step and a marker file. Under test are the Lima path (build, export, base, clone, grow,
   start, command, stop, delete) and not the 40 s package installation, which production has already passed. The run
   takes about 35 s and needs no internet beyond the cached image.
