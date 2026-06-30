# Execution Notes

Docker execution is the pipeline step that runs a submitted workflow inside an
isolated container.  Execution happens after ingestion and validation.  Scoring
and reporting are implemented in subsequent pipeline phases.

## How Execution v2 Works

| Step | What happens |
| --- | --- |
| Resolve command | Reads `run_config.json` from the submission if present; falls back to `python3 run.py` |
| Find Dockerfile | Uses `submission/Dockerfile` if present; otherwise uses `docker/Dockerfile.example` |
| Build image | Runs `docker build -t osipi-<challenge>-<name>:latest` |
| Mount submission | Mounts the submission folder at `/submission:ro` (read-only) |
| Mount output dir | Mounts `{run_dir}/outputs/` at `/output:rw` (read-write) |
| Apply resource limits | `--memory 4g`, `--cpus 2.0` |
| Apply security | `--network none`, `--security-opt no-new-privileges` |
| Enforce timeout | Kills the container after `timeout_seconds` (default: 300 s); sets `timed_out=True`, exit code 124 |
| Save logs | Writes combined build + run stdout/stderr to `{run_dir}/execution_stdout.log` and `execution_stderr.log` |
| Collect outputs | Scans `{run_dir}/outputs/` for NIfTI files after the run; returns as `output_files` |

## run_config.json

A submission can include an optional `run_config.json` at its top level to
specify the command to run inside the container:

```json
{
  "command": "python3 run.py --input /submission --output /output"
}
```

Only the `"command"` key is used.  If the file is absent or unparseable, the
default command `python3 run.py` is used.

## CLI Usage

```bash
PYTHONPATH=src python3 -m osipi_pipeline.execution.run \
  --input submissions/extracted/dce/dce_team_alpha \
  --challenge dce \
  --timeout 600 \
  --memory 4g \
  --cpus 2.0 \
  --command "python3 run.py --output /output"
```

## API Usage

```
POST /api/execute
{
  "submission_id": "dce_team_alpha",
  "challenge_type": "dce",
  "timeout_seconds": 300
}
```

Response includes the full `ExecutionResult` fields plus `stdout_preview` and
`stderr_preview` (first 8 KB of each log file).

## Fallback Dockerfile

`docker/Dockerfile.example` is used when a submission has no `Dockerfile`.
It is based on `python:3.11-slim` and pre-installs `nibabel` and `numpy` so
most perfusion-imaging scripts work without modification.

## Directory Layout

```
data/outputs/execution/
  {challenge}_{submission_name}/
    execution_stdout.log      # combined build + run stdout
    execution_stderr.log      # combined build + run stderr
    outputs/                  # mounted at /output inside the container
      *.nii.gz …              # files written by the submission
```

## Security Constraints

| Constraint | Flag |
| --- | --- |
| No network access | `--network none` |
| No privilege escalation | `--security-opt no-new-privileges` |
| Submission is read-only | `-v /submission:ro` |
| CPU limited | `--cpus 2.0` |
| Memory limited | `--memory 4g` |

## Docker-outside-of-Docker (DooD) Setup

The backend runs inside Docker.  To execute submissions it needs the Docker CLI
and access to the host Docker daemon via the socket.

### docker-compose.yml requirements

```yaml
services:
  osipi-backend:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
```

The socket mount is already present.  Do **not** use `privileged: true`.

### Dockerfile requirements

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip git docker.io \
    && rm -rf /var/lib/apt/lists/*
```

### Rebuild after Dockerfile changes

Always use `--no-cache` so apt-get actually re-runs:

```bash
docker compose build --no-cache osipi-backend
docker compose up -d
```

### Verify Docker CLI inside the container

The compose service name is **`osipi-backend`** (not `backend`):

```bash
# Docker CLI is on PATH
docker compose exec osipi-backend which docker

# Docker daemon is reachable via the mounted socket
docker compose exec osipi-backend docker version

# Socket is present inside the container
docker compose exec osipi-backend ls -l /var/run/docker.sock
```

Expected output for `docker version`:
```
Client: Docker Engine - Community
 Version: <host version>
 ...
Server: Docker Engine - Community
 Engine:
  Version: <host version>
```

If `Server` is missing or shows a connection error, the socket mount is not
working — check that `/var/run/docker.sock` exists on the host and that the
`volumes:` entry is present in `docker-compose.yml`.

## Current Limitations

| Limitation | Why it matters |
| --- | --- |
| Docker must be installed | Execution cannot run without the Docker command |
| Logs are local files | They are not yet summarised into challenge reports |
| Scoring is not implemented | Output NIfTI files are collected but not scored yet |
