# Current roadmap

The previous phase plans and validation notes are in [`old/`](old/). Their experiment
counts refer to data removed at the researcher's request. Use the current code and
new run artifacts to evaluate the present pipeline.

| Work | Current status | Reference |
|---|---|---|
| AIOpsLab contract adaptation for Docker Compose | Pilot implemented; further validation needed | [Refactor and validation record](../docs/AIOPSLAB_REFACTOR_PLAN.md) |
| Dataset collection and four-task evaluation | In progress; rebuild from new runs | [Agent CLI](../agent/README.md) |
| Web dashboard | Available; evaluate against the new campaign | [Web guide](../web/README.md) |

Keep one fault active per run, preserve generated experiment artifacts, and follow the
resource limits in [architecture](../docs/ARCHITECTURE.md).
