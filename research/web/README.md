# RCA web dashboard

Create and activate the project virtual environment first (the system Python on the homelab
does not necessarily include `pip` or FastAPI):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r research/requirements.txt
source .venv/bin/activate
```

Run the dashboard from the repository root. It binds to all host interfaces by default, like
the Compose-published frontend and observability ports:

```bash
python -m research.web --host 0.0.0.0 --port 8000
```

Open it in a browser:

```text
http://localhost:8000
http://<host-ip>:8000
http://<tailscale-host>:8000
```

The dashboard is intentionally open on the private host network. If the host is later exposed
to a shared or public network, configure a bearer token in the repository-root `.env`:

```dotenv
RESEARCH_GUI_AUTH_TOKEN=use-a-long-random-value
```

The GUI exposes the allowlisted `ready_for_pilot` runtime scenarios. Preparation validates the
stack and starts the required `loadgenerator` profile when it is stopped; a separate confirmation
then starts the fault. It permits only one active fault job. Code-level scenarios and catalogued
unavailable scenarios remain visible for context but cannot be executed from the GUI.

The live trace is sanitized before it is streamed to the browser. Ground truth is not sent to
the agent and is only available in a completed run detail. Agent session events and GUI job events
are stored as append-only local research artifacts.
