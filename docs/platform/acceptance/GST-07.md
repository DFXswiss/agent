# GST-07 acceptance (platform tools, pending calls, platform queries)

All tests in `tests/platform/guest/mcp_tools/test_tools.py` unless named otherwise.

| Criterion | Evidence |
|---|---|
| All tools of 03 §8.1 registered, allow-list of the managed settings matches (SBX-01) | `test_registry_holds_every_tool_of_the_contract` (registry ∪ `get_notifications` = `sandbox.settings.MCP_TOOLS` = `policy.PLATFORM_TOOLS`), `test_tools_list_names_the_managed_settings_tools` (every listed tool allowed in `build_settings()`, contract-valid, plain object input schema) |
| Valid and invalid arguments (contract fixtures) | `test_every_tool_has_contract_fixtures`, `test_invalid_arguments_are_params_invalid_and_not_forwarded` (every invalid input fixture of every tool plus boolean/float/range/extra/session-argument cases and the `get_whiteboard` `task_id` rule; nothing journaled), `test_valid_arguments_are_forwarded_and_answered` (every valid input fixture) |
| Forwarding and answer | `test_valid_arguments_are_forwarded_and_answered` (`platform.query` with `pending_id`, tool, arguments, contract-valid journal body; fake runner answers through `POST /v1/pending/{id}/answer`; result as `content:[{type:"text",text:<JSON>}]`), `test_runner_errors_are_passed_on`, `test_waiting_read_answered_late_by_the_runner` |
| Output schemas checked | `test_every_valid_output_fixture_passes_and_every_invalid_one_is_refused`, `test_the_answer_route_refuses_a_result_of_no_tool`, `test_an_answer_about_another_object_is_unavailable` |
| Time limits (`pending`, `timeout`) | `test_unanswered_reads_time_out_after_the_handler_limit`, `test_reads_wait_ten_seconds`, `test_waiting_reads_wait_for_the_runner_up_to_the_route_limit` (`wait_seconds` + 10 s, ≤ 33 s), `test_read_limits_follow_the_specification`, `test_request_operation_without_an_answer_is_pending` |
| `request_operation` | `test_request_operation_forwards_and_returns_the_final_state` (UUIDv7 `operation_id`, `idempotency_key`, contract-valid `operation.request`), `test_request_operation_returns_every_outcome_of_the_runner`, `test_request_operation_local_check_forwards_and_rejects` (unknown type, path-like type, boolean as integer, extra and missing params: journaled and `rejected` at once), `test_local_check_drops_overlong_pointers`, `test_every_operation_type_of_the_index_is_known`, `test_request_operation_answer_for_another_operation_is_unavailable`, `test_request_operation_invalid_result_and_runner_error` |
| Foreign session tokens, session only from the token | `test_foreign_or_missing_session_tokens_are_refused_before_anything_is_journaled`, `test_a_stopped_session_token_is_refused`, `test_the_session_comes_from_the_token_only` (two sessions; journal records carry the token's session) |
| Journal records | every forwarding test reads the journal (`forwarded(context)`): kind, session, body |
| Child check of `get_subagent_result` | `test_get_subagent_result_refuses_the_own_session` (not forwarded), `test_get_subagent_result_child_check_is_the_runners` (non-child `not_found` from the runner passed on) |
| Control process unreachable | `test_an_unreachable_control_process_is_unavailable`, `test_a_locally_rejected_operation_needs_the_control_process`, `test_forwarding_for_an_inactive_session_is_unavailable` |
| Contract defect fixed (agent-core) | `contracts/changes/GST-07.md`; agent-core `tests/platform/contracts/test_ctr02.py::test_tool_inputs_are_plain_objects`, `test_whiteboard_task_id_rule_is_left_to_the_implementations` |
| E2E-19/21/22/24, ATK-33/36/37 | not yet written (SCN-04, ATS-02); decisions GST-07 no. 14 |
| Gates | `tools/wp/gates.sh GST-07` (agent and agent-core) green: ruff, `mypy --strict`, 100 % lines and branches, standard library only |
