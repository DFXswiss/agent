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
4. **The real test runs in the gates and does not carry the `vm` marker.**
   `tests/platform/runner/workvm/test_real_lima.py` follows `test_lima_accepts_the_template`. It runs wherever real
   `limactl` 2.3.0 or later is installed on macOS, which covers the test devices in `platform-test`. Elsewhere it is
   skipped with a reason (CI runners, Linux). A missing `AGENT_RUNNER_VM_PREFIX` fails the test. The suites marked
   `vm` deselect themselves through their `conftest.py` unless the run asks for `-m vm`, so they never run in
   `make platform-ci`. This package needs the opposite: a new Lima version has to show up in the gate. The test starts
   one small VM (2 CPUs, 2 GiB) for under a minute, inside the gate's own slot. It deletes only its own instances (by
   their name kinds under the package prefix).
5. **A small template, the production clone settings and a stand-in provisioning script.**
   - The image comes from `images/workvm.yaml` with its pinned image and every Lima setting, at 2 CPUs, 2 GiB and
     10 GiB.
   - The clone takes the expressions of `WorkVms._sets` for the pool size: 50 GiB disk, pinned SSH port, resolvers.
     It only adds a later `--set` with small CPUs and memory, so the clone also proves that the disk grows from
     10 GiB to 50 GiB.
   - The provisioning script installs the three commands of the smoke step and a marker file.
   - Under test is the Lima path: build, export, base, clone, grow, start, command, stop, delete. The 40 s package
     installation is left out; production has already passed it.
   - The run takes 35–50 s and needs no internet beyond the cached image.
6. **`metrics/vm.py` stays as it is.** It names `disk` for the allocated size of workgroup VMs. That module belongs to
   another package, does not touch work VMs, and is already correct for Lima 2.x.
