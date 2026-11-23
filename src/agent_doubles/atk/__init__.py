"""Attack suite ATK-01 … ATK-32 (docs/platform/spec/07-testing.md section 2.3, 05-runner.md section 4, ADR-008).

Real Claude Code sessions in a Lima VM cloned from a TEST_MODE golden image are driven by the scripted adversary
scenarios of the model double (``agent_doubles/model/scenarios/atk-NN.json``). Every attack runs twice: with the
guest hooks active (mode ``hooks``) and with the hooks forced to HTTP 500 plus poisoned user and project settings
(mode ``fault``). A run passes when the evidence shows no execution, no write outside the workspace, no read of
foreign data, no egress, and every attempt journaled.

| Module | Content |
|---|---|
| ``catalog`` | the 32 attacks: group, scenario, the paths each attack targets, the scripted tool calls |
| ``guest_probe`` | stdlib script run as root inside the VM: file-system snapshots, exact target states, nftables
  counters, process samples, transcripts (copied into the VM by the driver) |
| ``evidence`` | pure evaluation of one run's evidence into a verdict per criterion |
| ``dispatch`` | host-side HTTP dispatcher on the golden's test model port: one model double per session |
| ``driver`` | VM preparation, session start through the guest control link, evidence collection, parallel runs |
| ``report`` | the result table (Markdown and JSON) |

The package uses the standard library only; the runner pieces it drives (VM provisioning and the guest control
client of ``agent_runner``) are injected by the caller (``tests/platform/atk/``), like every other double.
"""
