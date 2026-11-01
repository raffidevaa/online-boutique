# Dataset reset and pilot status

On 2026-10-30, the previous generated experiment runs, agent sessions, and Phase 04–06
evaluation bundles were removed at the researcher's request. The experiment schema,
scenario catalog, and `.gitignore` files remain. Historical Phase 04–06 numbers in older
documentation describe the retired campaign and must not be cited as results of the new one.

The current workflow uses one RCA evidence-gathering pass followed by a separate
recommendation output. Recommendation receives the RCA submission and recorded
action/observation trace, and has no telemetry actions. Ground truth is supplied to evaluation
only after both outputs are complete.

## Replacement pilot

At the researcher's request, the prior run `EXP-20261030T163934501850Z-562609` and its
index row were removed before the replacement pilot.

Run `EXP-20261030T170130648656Z-13f7fc` used scenario `FI-RES-CPU-01` and the configured
DeepSeek provider. RCA identified `recommendationservice` correctly but classified the fault
as `network_delay` instead of the ground-truth `resource` / `cpu_hog`. Recommendation reused
the recorded RCA observations, made one submission, and chose `manual_investigation` because
the evidence did not support a safe network configuration change. The run completed with
recovery recorded and is eligible in the generated dataset index; human recommendation review
remains pending.
