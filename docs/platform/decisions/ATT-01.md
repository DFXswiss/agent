# ATT-01 – files and images attached to prompts

Package of [E-43](../KONZEPT.md#e-43); architecture in [ADR-102](../adr/102-prompt-attachments.md). Small decisions
taken while building it:

1. **One chunk size for every file (1 MiB).** Large enough that a screenshot is one or two requests, small enough
   that a chunk upload stays well below the 4 MiB batch body limit as b64u. The 32 MiB file limit then caps an object
   at 32 chunks, so the hub needs no per-object quota of its own: the index bound (`0..31`) and the chunk size keep
   every object within the sealed size of a 32 MiB file.
2. **No foreign key from `attachment_object.wg_id` to `wg`.** `wg` is a projection of the membership log that the
   rebuild of `agent_hub.backup.rebuild` recreates; a cascade would delete every attachment on a rebuild.
3. **Upload at send time, not when the file is chosen.** A file the person removes again or never sends leaves
   nothing at the hub. The references of files already uploaded are kept for a retry to the same workgroup (the
   identical prompt is re-sent); for another workgroup the file is uploaded anew.
4. **Pictures as `data:` URLs.** The hub's CSP allows `img-src 'self' data:`; `blob:` would need a CSP change.
   Downloads use the existing `saveBlob` (an anchor with `download`), which the CSP does not restrict.
5. **The first prompt with files is not put on the command line.** The start of a session happens before the
   workspace can take files; leaving the prompt `pending` lets the regular delivery import the files and type it,
   with no change to the start sequence.
6. **Names in the workspace are numbered and reduced** (`<n>-<name>` with `[A-Za-z0-9._-]`): two files of one
   prompt never collide, the typed paths need no quoting, and no file takes a protected name of the guest's import.
7. **Hub client looked up when needed.** The runner's fetcher asks for the sync runtime's client at the first chunk
   (`prompts.attachments.hub_of`); a device that is not enrolled yet backs off instead of failing.
8. **Agent tool parity later.** The agent tool keeps sending text prompts; attaching files with it is a package of
   its own ([E-04](../KONZEPT.md#e-04)).
