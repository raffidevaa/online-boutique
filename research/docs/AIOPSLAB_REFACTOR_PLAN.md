# AIOpsLab contract refactor for Docker Compose

## Goal and compatibility boundary

Align the problem lifecycle, agent contract, sessions, actions, and evaluation for detection, localization, analysis, and mitigation with AIOpsLab. Docker Compose remains the only deployment runtime. Thesis fault labels remain authoritative, so Analysis taxonomy is an explicit extension rather than an exact match to upstream AIOpsLab. Do not modify upstream Online Boutique files or existing experiment artifacts.

## Refactor sequence

1. **Problem definition:** bind application, task, one fault scenario, workload, and evaluator in the registry. Keep existing scenario IDs and experiment artifacts. Status: `ProblemDefinition` is used by registry and orchestrator; live lifecycle still needs to consume it directly.
2. **Orchestrator lifecycle:** make one owner responsible for preflight, application and workload setup, fault injection, agent loop, evaluation, recovery, and session persistence for live runs. Replay uses the same problem and session contracts without injecting a fault. Always recover on failure. Status: CLI enters through `AgentOrchestrator.run_live_problem`, which validates the registered problem; Compose lifecycle remains in its controller adapter.
3. **Runtime interfaces:** separate application, injector, workload, observer, and environment actions from their Compose/Pumba implementations. Keep Kubernetes dependencies out of research code. Status: live application, fault, telemetry, workload, and target-scoped mitigation action contracts are implemented.
4. **Agent and session contracts:** support `async get_action(state)`, registration, one parsed action per step, task-specific `submit`, and consistent history, solution, timing, results, and termination status. Preserve existing read-only telemetry actions. Status: four tasks are registered; localization accepts the upstream list signature while retaining the thesis result shape. Sessions expose both upstream-style `history`/`final_state` and the thesis `trace` field.
5. **Mitigation execution:** add Compose operations through an explicit allowlist scoped to the active experiment. Judge mitigation using post-action service state and fault recovery. Retain the existing recommendation as supplementary output. Never expose arbitrary shell or container commands. Status: live mitigation exposes target-scoped `restore_service` and `submit()`; the operation stops the active fault before recreating its container. A live productcatalogservice run completed successfully.
6. **Evaluation and artifacts:** use `eval(solution, trace, duration)` for all tasks, common session metrics, and one session schema for replay/live. Document non-comparable upstream metrics. Treat prior experiment files as append-only. Status: all four task classes use the evaluator contract and live evaluation includes `task` plus `task_results`; mitigation success requires an agent-stopped fault and a running target at submission. Telemetry-based recovery scoring remains separate work.

## Known compatibility differences

- Analysis retains the thesis resource/network/code-level taxonomy. Upstream AIOpsLab uses broader system-level and fault-type labels, so scores cannot be directly compared without a documented mapping.
- The framework exposes no arbitrary shell or container commands to the agent. This follows the repository's allowlist requirement, even where upstream AIOpsLab provides broader operational actions.
- Existing `remediation` is a recommendation task and remains separate from executable `mitigation`.
- Upstream token-usage and qualitative-judge scores are not claimed when the configured provider does not return usage data or a judge is not configured.

## Verification

- Contract tests for four tasks, action parsing, submission, registry, session serialization, and evaluator results.
- Replay regression against validated scenarios, comparing RCA output before and after refactor.
- One live noncritical single-fault scenario, including forced failures at each lifecycle phase and recovery verification. A productcatalogservice mitigation run completed; failure injection across all lifecycle phases remains.
- Validate Compose syntax with `docker compose config`; after Compose changes, start services and verify running/healthy state, especially adservice.

## Validation record

- `python -m unittest discover -s research/tests -p 'test_*.py' -q` with the project virtual environment: 94 tests passed.
- `docker compose -f research/compose/docker-compose.yml config --quiet`: passed.
- Live `FI-NET-LOSS-02-mitigation` on productcatalogservice: completed, the agent stopped the fault and restored the target; 22 containers were running with no unhealthy state afterward.
- An earlier validation run exposed a Pumba network-namespace error when the target was recreated before stopping the fault. Its experiment artifact remains append-only with `injector_failed`; the action order was fixed and the subsequent run completed.
- Replay comparison against the previously validated data is unavailable because the old experiment data was removed at the user's request. The new live runs are retained under `research/experiments/runs/`.
