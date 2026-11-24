# Threat model: platform browser and its live picture (ADR-085, 13-work-vm.md §13)

Extends the threat model of the work VM ([work-vm-threat-model.md](work-vm-threat-model.md)); everything said there
about the VM boundary holds for a browser VM, which is a work VM of scope `browser`.

## Assets
1. The content a session sees and does in its platform browser: pictures of pages, URLs, page text, typed text
   (passwords of sites the session signs in to), downloaded files.
2. The confidentiality of the workgroup towards the hub (zero knowledge, ADR-003): the hub must not learn assets 1.
3. The execution device, the site's internal network and other VMs (as for the work VM).
4. The runner and the session sandbox (answers of the helper, the record, the files brought into the workspace).

## Attackers
- **A1 – the page**: any web site the session opens (prompt injection in the page, malicious scripts, exploits of
  Chromium) and therefore anything running as root in the browser VM: the helper `workvm-browser`, the Lima guest
  agent, the kernel.
- **A2 – the hub operator** (honest but curious, or a hub that was taken over): sees every request, every stored row
  and every live frame that passes.
- **A3 – a member without rights**: an authenticated user or agent of the organization who does not read the session's
  workgroup or task, or a user of another organization.
- **A4 – a network observer** between device, hub and browser.

## Threats and controls
| # | Threat | Control | Verified by |
|---|---|---|---|
| B1 | A1 reaches the device, the site network or other VMs from the browser VM | The browser VM is a work VM: slot account, host filter, egress only through the Cloudflare tunnel, fail-closed; known limit 13-work-vm.md §6.6 (accepted 2026-11-24) | WVM-01…10 (ATK), E2E-66 probes from the browser (host loopback, the host's address, a private address) |
| B2 | A1 attacks the runner through crafted helper answers (huge answers, wrong schema, endless frames) | Every answer bounded (header 4 KiB, call answer 4 MiB, frame 512 KiB), closed schema, answers become text and files for the session only; a frame is opaque bytes for the runner (sealed, never decoded); the byte budget bounds the frame rate | runner tests `test_browser_helper.py` |
| B3 | A1 plants files in the workspace through downloads or screenshots | The runner writes exactly one regular file at the session's chosen workspace path through the guest import (no links, no specials, no `..`, protected names refused, size limit) | runner tests, guest import tests |
| B4 | A1 injects instructions into the session through page content | Same class as any untrusted content the session reads (ATK-34/40); the sandbox stays closed; the browser holds no platform credential | existing ATK cases |
| B5 | A1 learns platform credentials | None placed in the browser VM (as the work VM); the browser profile starts empty and is deleted with the VM | WVM-06 |
| B6 | A2 reads pictures, URLs, typed text or page text of the record | `browser.action` and `browser.state` are events: AES-256-GCM under `k_event` of the workgroup key, signed by the device; the hub stores ciphertext | zero-knowledge scan of E2E-66, crypto vectors |
| B7 | A2 reads live frames | A frame is sealed under `k_frame` (workgroup key, own label, random salt, AAD binding browser, session, workgroup, epoch, seq, time); the hub keeps only the latest ciphertext per session, unlogged, and drops it with the watch | `browserframe.json` vectors, zero-knowledge scan |
| B8 | A2 swaps or forges frames (a picture of another session, an old picture, a made-up picture) | Without `WK_e` no valid frame can be made; the AAD binds session and workgroup (a frame of another session or workgroup does not open); the viewer shows a frame only with a `seq` above the last one and shows its age, so a replayed old frame is not shown as new | web tests `browserframe.test.ts` |
| B9 | A2 learns metadata | It sees that a browser session exists (operation record, `browser.state` sizes), when actions happen (event times and sizes), frame sizes and rates while watched, and who watches. Accepted: the same metadata class as terminal output and events (04-crypto.md §7) | — |
| B10 | A3 watches a session's browser or reads its record | `browser=` needs `task:read` of the session (else `404`, like `terminal=`); `browser_action` and `browser_live` frames reach only streams watching that session; the record is read with the events route of the workgroup (`wg:read`); audit `read.stream.browser` | hub tests of the stream, E2E-66 foreign task |
| B11 | A3 makes a device stream (bandwidth abuse) | Watching needs `task:read`; a lease is per watched session and bounded to 3 per stream; the device caps the rate (`BROWSER_LIVE_MAX_KBPS`, 5 frames per second) | hub and runner tests |
| B12 | A device posts frames of a session it does not run, or into another workgroup | The live route takes only sessions running on the calling device in a workgroup it is assigned to, and `frame.session`/`frame.wg` must name the path's session and its workgroup | hub tests |
| B13 | A4 reads frames in transit | TLS to the hub plus end-to-end encryption of every frame and event | — |
| B14 | The record shows a password typed into a sign-in form to every reader of the browser view | The runner replaces the typed text of a password field by `••••` in `browser.action`; the picture shows the field masked as the page does. Not a protection against readers of the workgroup in general: the session's `session.tool` record holds the tool input like every input of the session (its own content, ADR-016) | runner tests, E2E-66 |
| B15 | Bandwidth without a viewer | The device starts the screencast only on the hint of a lease; a process deletes its lease when its last watching stream ends; the device posts at least every 10 s and stops on `409 not_watched`, so at most 10 s after the last viewer left | runner and hub tests, E2E-66 (no frame without a viewer) |
| B16 | A slow viewer is flooded (or makes other viewers resync) | Frames do not queue at the hub: one slot per stream and watched session, outside the stream buffer, sent within the stream's own `browser_kbps`; `browser.action` is not replayed on reconnect | hub tests |

## Not covered (accepted)
- A viewer with `task:read` sees the pictures of sites the session signed in to, including what those sites show
  (account pages). That is the purpose of the live view (idea 28).
- The browser VM shares the known limit of the per-account host filter (13-work-vm.md §6.6).
- A holder of the workgroup key (a member or device of the workgroup) working together with the hub could make a live
  frame (frames are not signed, ADR-085 §6); the stored record is signed by the device.
- The browser VM cannot reach the session's own work VM (outside boundary); looking at an app in the work VM goes
  through the unwatched `workvm_browse` (E-09).
- Web JavaScript is delivered by the hub (06-web.md §12): a taken-over hub that ships changed JavaScript can read what
  the browser decrypts; the same limit as every web end-to-end encryption.
