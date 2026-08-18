# Execution notes

The Run step uses Docker only for reproducible submissions. Submissions that
already contain result maps skip execution.

## What happens

1. Read `run_config.json` when present; otherwise run `python3 run.py`.
2. Require a submitted `Dockerfile`.
3. Build a local image.
4. Mount the submission read-only at `/submission`.
5. Mount the output folder read-write at `/output`.
6. Run without network access and with CPU, memory, and time limits.
7. Save the logs and collect generated NIfTI files.

Example `run_config.json`:

```json
{
  "command": "python3 run.py --input /submission --output /output"
}
```

Only `command` is used. Invalid or missing files fall back to the default
command.

## Limits and security

The default timeout is 300 seconds. The runner also uses:

- `--network none`
- `--security-opt no-new-privileges`
- `--cpus 2.0`
- `--memory 4g`

The app container uses the host Docker socket to start participant containers.
The Compose file already mounts `/var/run/docker.sock`; do not add
`privileged: true`.

Check Docker access from the app container with:

```bash
docker compose exec osipi-backend docker version
```

Generated files are stored under `data/outputs/execution/`. Docker must be
running for reproducible submissions. Full logs remain local.
