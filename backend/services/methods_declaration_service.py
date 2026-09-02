"""What the submitter said about their methods document.

It used to be required outright for DCE: a submission without one failed
validation. The challenge leads have since said it is not needed for the runs
being done now, so the blanket requirement is gone. What is left is narrower
and cannot be wrong about their policy:

* the upload form asks whether a methods document is included;
* the answer is recorded here, beside the submission rather than inside it, so
  that a manifest rebuild cannot lose it;
* a submitter who answers yes and sends none gets an error, because that is a
  mismatch between what they said and what arrived;
* a submitter who answers no, or who is never asked, is not stopped, and
  nothing is added to their submission.

That last point was a deliberate reversal. An earlier version dropped a blank
template into any submission that declared no document, so that every
submission would have the file. It was withdrawn because it answered a question
nobody had asked: we do not know whether a team has a methods document of their
own, or wants ours, and a submission that arrives without one has to move
through the pipeline untouched either way. The template is still offered as a
download on the upload screen -- take it or leave it -- and this module never
writes into a submission.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.path_config import OUTPUTS_DIR, PROJECT_ROOT

#: Where the blank template lives in the repository. It is offered for download
#: and never copied into anyone's submission.
TEMPLATE_PATH = PROJECT_ROOT / "config" / "methods_template.txt"

#: The three answers. ``None`` means the submitter was never asked, which is not
#: the same as answering no and must not be reported as if it were.
PROVIDED = "yes"
NOT_PROVIDED = "no"
UNDECLARED = None


def _safe_name(submission_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(submission_id))
    return safe.strip("_") or "submission"


def _declarations_dir() -> Path:
    return Path(OUTPUTS_DIR) / "declarations"


def _declaration_file(submission_id: str) -> Path:
    return _declarations_dir() / f"{_safe_name(submission_id)}.json"


def normalize(value) -> Optional[str]:
    """Turn whatever the form sent into ``"yes"``, ``"no"`` or ``None``.

    Anything unrecognised becomes ``None``: an answer we cannot read is an
    answer we do not have, and guessing which way it went would be worse than
    recording that nobody said.
    """
    if value is None:
        return UNDECLARED
    if isinstance(value, bool):
        return PROVIDED if value else NOT_PROVIDED
    text = str(value).strip().lower()
    if text in {"yes", "true", "1", "provided", "y"}:
        return PROVIDED
    if text in {"no", "false", "0", "not_provided", "n"}:
        return NOT_PROVIDED
    return UNDECLARED


def record(submission_id: str, declared) -> dict:
    """Store the submitter's answer. Returns the record as written."""
    entry = {
        "submission_id": str(submission_id),
        "methods_document_declared": normalize(declared),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _declaration_file(submission_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    return entry


def load(submission_id: str) -> dict:
    """Read the recorded answer, or an empty declaration if there is none."""
    empty = {
        "submission_id": str(submission_id),
        "methods_document_declared": UNDECLARED,
        "recorded_at": None,
    }
    path = _declaration_file(submission_id)
    if not path.exists():
        return empty
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # An unreadable record is not a declaration. Reporting "not provided"
        # here would be inventing an answer out of a disk error.
        return empty
    if not isinstance(stored, dict):
        return empty
    empty.update({
        "methods_document_declared": normalize(stored.get("methods_document_declared")),
        "recorded_at": stored.get("recorded_at"),
    })
    return empty


def template_text() -> str:
    """The blank template, read from the repository, for the download route."""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def summary(submission_id: str, methods_files: Optional[list] = None) -> dict:
    """One description of the methods situation, for validation and reports.

    ``methods_files`` is whatever the scan found. It catches a submitter who
    declared a document and did not send one, and it is reported alongside the
    declaration. Any file found is the submitter's own -- nothing here puts one
    there -- so a file found is a document found.
    """
    declared = load(submission_id)["methods_document_declared"]
    found = [str(item) for item in (methods_files or [])]

    if declared == PROVIDED:
        status = "provided" if found else "declared_but_missing"
    elif declared == NOT_PROVIDED:
        status = "not_provided"
    else:
        status = "provided" if found else "not_declared"

    labels = {
        "provided": "Provided",
        "declared_but_missing": "Declared, but no document was found",
        "not_provided": "Not provided (declared by the submitter)",
        "not_declared": "Not provided (the submitter was not asked)",
    }
    return {
        "status": status,
        "label": labels[status],
        "declared": declared,
        "documents_found": found,
        "recorded_at": load(submission_id)["recorded_at"],
    }
