"""Installation of the runner on an own Mac without a terminal (ADR-074, 05-runner.md section 16).

`layout` names the installed paths, `release` the signed manifest of a package, `preflight` the checks of the Mac,
`owner` the Mac's owner the runner runs as, `system` what the helper takes from the host, `daemons` the two
LaunchDaemons, `state` the public state file, `update` the updates with a way back, `admin` the one administrator
step of the agent way, `build` the package build, and `root` the privileged helper that the package's `postinstall`,
the administrator step of `agent-platform device add|remove` and the maintenance daemon run.
"""
