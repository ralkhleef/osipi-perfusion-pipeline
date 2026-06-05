# Execution Notes

Docker execution is the pipeline step that runs a submitted workflow in a container.

In this project, execution happens after ingestion and validation. It does not score outputs or create reports yet.

## Why Docker Is Useful

Docker helps make challenge runs more reproducible.

| Benefit | Meaning |
| --- | --- |
| Same environment | Code runs with the same installed tools each time |
| Isolation | Submission code runs inside a container |
| Easier debugging | Build and run logs can be saved |
| Future automation | Later scoring can use the same execution step |

## How Execution v1 Works

| Step | What happens |
| --- | --- |
| Find Dockerfile | Uses the submission `Dockerfile` if present |
| Use fallback | Uses `docker/Dockerfile.example` if the submission has no Dockerfile |
| Build image | Runs `docker build` |
| Mount submission | Mounts the submission folder at `/submission` inside the container |
| Run placeholder | Runs `echo "OSIPI execution placeholder"` |
| Save logs | Writes stdout and stderr logs to `data/outputs/execution/` |

## Example Command

```bash
PYTHONPATH=src python3 -m osipi_pipeline.execution.run \
  --input submissions/extracted/dce/dce_team_alpha \
  --challenge dce
```

## What Is Not Implemented Yet

- Real submitted run commands
- Challenge-specific runtime arguments
- Output file validation after execution
- Scoring
- Reporting
- Resource limits for CPU, memory, or runtime

## Future Plan

Later versions should let each challenge define the real command to run.

For example, the pipeline may eventually read a small config file that says how to run a submitted workflow and where outputs should appear.

## Current Limitations

| Limitation | Why it matters |
| --- | --- |
| Docker must be installed | Execution cannot run without the Docker command |
| Fallback image is only a placeholder | It is not a scientific execution environment |
| The command is only an echo | No real MRI processing happens yet |
| Logs are local files | They are not summarized into reports yet |
