# Configuration Walkthrough — Narration Script

**Length:** about 2 minutes

## Opening

This is the OSIPI perfusion challenge submission pipeline. The documentation and application are designed to make the workflow clear for both technical maintainers and scientific reviewers.

## Submission rules

Challenge requirements are loaded from one file, `config/validation_rules.yaml`, and read at runtime rather than compiled into the application.

Under `map_types`, each entry gives a map its display name, units, expected dimensionality, and the filename `patterns` used to recognise it — so adding an alias such as Perfmap for CBF is a one-line change here, followed by a focused validation test. Under `challenges`, `required_maps` and `optional_maps` decide what a submission must contain, `required_artifacts` covers non-map files such as the modelled signal-time curve and the methods document, and `datasets` states the expected participant, repeat and site grid. The file is validated when it loads, so a typo fails immediately rather than being silently ignored.

## Scoring providers

Approved scoring integrations are registered in `backend/scoring.py`. Each provider identifies its challenge, map type, official status, metrics, scoring script, reference-data folder, and mask folder.

A new metric should not be added only by name. The team should first confirm the formula, units, ROI or voxel aggregation, pairing rules, and expected output format. The adapter should then return the real artifacts created by the approved script. The pipeline should never generate placeholder scientific scores.

## Private reference data

Provider paths are defined in `backend/services/path_config.py`. Hidden masks, clinical data, or organizer-owned reference files should not be committed to the public repository. They can be copied into the provider folder locally or mounted securely for the evaluation environment.

## Participant execution

For reproducible submissions, a participant may include `run_config.json` to define the command and timeout. The Docker container writes generated maps to the output directory, which can then be validated and passed to an enabled scoring provider.

## Documentation and publishing

The project documentation is a simple static site in the `docs` folder. Screenshots, this video, and the configuration guide are deployed by the GitHub Pages workflow in `.github/workflows/pages.yml`.

After reviewing the repository for private data and generated outputs, push the documentation and workflow to the main branch. In GitHub Pages settings, choose GitHub Actions as the publishing source.

## Closing

The core workflow is working. The main remaining task is to convert the final mentor-approved DCE and ASL scientific definitions into tested scoring providers and report fields.
