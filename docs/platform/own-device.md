# Connect your own Mac

Tasks of agent.dfx.swiss run on DFX execution devices by default. You can also let them run on your own Mac, for
example a Mac Studio (ADR-074). You never need a terminal for it.

## What your Mac needs

- macOS 14 or newer on Apple silicon (M1 or newer), 16 GB of memory or more, 50 GB of free space.
- An administrator account: macOS asks for its password once.

## With your AI agent

Tell your agent: "Connect this Mac as an execution device." It checks the Mac, downloads the signed DFX package,
tells you in one sentence what the next dialog means and starts it. Enter your password (macOS may offer Touch ID).
A few minutes later the Mac appears on the devices page of your organization, assigned to the workgroup you named
(by default the default workgroup). The first task there takes longer: the Mac prepares its VM image once.

To remove it, tell your agent: "Remove this Mac as an execution device." It unpairs the Mac and asks once more for
your password; nothing of the installation stays behind.

## Without an agent

1. Open the devices page of your organization and choose **Add device**, then **Download for Mac** (shown once DFX
   publishes a notarised package; until then let your AI agent connect the Mac). The page shows a device code; keep
   the page open.
2. Open the downloaded package and follow the installer; it asks for your password.
3. When the installer asks for the device code, paste the code from the page.

To remove the Mac, choose **Unpair** on the devices page. The Mac removes its installation by itself within a quarter
of an hour.

## Good to know

- Whoever administers your Mac can read the content of every task it runs for your organization – also of workgroups
  you are not a member of – including what stays on the Mac after a task ran. Assign the Mac only where that is fine.
- Your Mac runs tasks only inside separate virtual machines. The service runs under your account and does not need to
  be logged in to work; when the Mac sleeps, its tasks wait.
- The service updates itself at night when no task runs, and goes back to the previous version when an update does not
  work.
- If you switch off "DFX Agent Runner" (shown as `dfx-agent-runner`) in **System Settings → General → Login Items**, the
  Mac stops working for your organization; switch it on again to continue.
