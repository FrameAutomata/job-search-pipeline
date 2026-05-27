"""Local web UI for the job-search pipeline.

A FastAPI app that runs on localhost and serves a browser UI for triaging
evaluation results (and, in later phases, onboarding + running the pipeline).
Everything stays on the user's machine — no server, no external service.

Launch with `run-ui.sh` / `run-ui.ps1`, or:
    uvicorn pipeline.app.server:app
"""
