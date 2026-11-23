# GST-03 acceptance (hooks, PreToolUse policy, prompt origin)

| Criterion | Evidence |
|---|---|
| Table-driven policy tests for every rule incl. symlink, hard link, `..`, `//`, case variants (ATK-13..20, 34, 35 as unit cases) | `tests/platform/guest/hooks/test_policy.py::test_policy_table` (85 named cases on a real tmp tree: absolute/relative/chained/dangling/looping symlinks, `..` after symlinks, hard links, `//`, trailing `/`, upper case, protected names, `~`, NUL, pattern forms), rule 1 for all 12 platform tools, rule 2 names, resolver error paths (permission, readlink, > 40 links), `resolve` vs `os.path.realpath` |
| Prompt origin: wrapper, NFC, double use, foreign session | `test_prompts.py` (`test_normalise` incl. forged wrappers, `test_registered_prompt_is_accepted_once`, `test_prompt_of_another_session_is_refused`, `test_registration_rules`, route and op tests); end to end through the hook in `test_hooks.py::test_user_prompt_submit_*` |
| All 18 events | `test_hooks.py::test_every_event_answers_by_contract_and_is_journaled`, `test_every_valid_contract_fixture_is_answered`, `test_invalid_bodies_fail_closed` (all contract fixtures of `contracts/fixtures/mcp/hooks/`) |
| 401 paths | `test_hooks_need_the_session_token` (none, wrong scheme, empty, unknown), `test_two_authorization_headers_are_refused`, `test_token_of_a_stopped_session_is_refused` |
| Fault switch, ineffective in production builds | `test_fault_switch_answers_500_in_a_test_golden`, `test_fault_switch_has_no_effect_in_a_production_golden` (missing, false, non-bool, broken, production record), `test_fault_switch_configuration` |
| Answers against `contracts/mcp/` schemas | every hook test validates the answer against `mcp/hooks/<Event>.response.json`; journal bodies against `guest/journal/session.hook.json`; deny texts against `hook-deny-reasons.json` and `mcp/objects.json#/$defs/deny_reason` |
| Addon mechanism (never on Stop) | `test_addon_notice_is_attached_to_tool_and_prompt_answers`, `test_addon_text_is_never_attached_to_a_block_or_to_stop`, `test_failing_addon_never_changes_the_decision`, `test_bad_addon_text_is_dropped`, `test_load_addons` |
| Gates | `tools/wp/gates.sh GST-03`: ruff, `mypy --strict`, standard library only, 100 % lines and branches (macOS + Linux combined) |
