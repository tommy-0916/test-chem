"""Seal reviewed material-relationship bindings into a versioned authority.

This command performs no binding discovery and makes no model call.  It binds
an operator-reviewed draft map to the exact Research package, Device-plan
operations and workstation-truth snapshot used for an offline repair replay.
Manual mode also verifies the independent review document and embeds the
selected records plus their digests so runtime replay can resolve them without
trusting an unfetched path string.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

if __package__ in (None, ""):
    _here = Path(__file__).resolve().parent
    _root = _here.parent
    for _path in (str(_here), str(_root)):
        if _path not in sys.path:
            sys.path.insert(0, _path)
    from material_relationship_compiler import (  # type: ignore
        relationship_binding_records,
        seal_relationship_binding_authority,
    )
    from plan_repair import _offline_full_plan_context  # type: ignore
else:
    from .material_relationship_compiler import (
        relationship_binding_records,
        seal_relationship_binding_authority,
    )
    from .plan_repair import _offline_full_plan_context


def _read_object(path: Path, label: str) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must contain a JSON object")
    return value


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--research-authority", required=True)
    parser.add_argument("--draft-bindings", required=True)
    parser.add_argument("--device-state", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--authoring-mode",
        choices=["manual_evidence_bound", "automated_evidence_bound"],
        required=True,
    )
    parser.add_argument("--research-evidence-source", required=True)
    parser.add_argument("--device-evidence-source", required=True)
    parser.add_argument("--workstation-evidence-source", required=True)
    parser.add_argument(
        "--manual-support-source",
        help=(
            "Distinct reviewed evidence source containing the manual operation-"
            "support decisions. Required in manual_evidence_bound mode; each "
            "positive draft record must already carry a support_evidence_ref "
            "under this source, and the source document must contain the "
            "matching operation_binding_reviews record."
        ),
    )
    args = parser.parse_args(argv)

    candidate_path = Path(args.candidate).expanduser()
    research_path = Path(args.research_authority).expanduser()
    draft_path = Path(args.draft_bindings).expanduser()
    device_state_path = Path(args.device_state).expanduser()
    output_path = Path(args.output).expanduser()

    if args.authoring_mode == "manual_evidence_bound" and not str(
        args.manual_support_source or ""
    ).strip():
        parser.error(
            "--manual-support-source is required for manual_evidence_bound"
        )

    candidate = _read_object(candidate_path, "candidate")
    research_authority = _read_object(research_path, "research-authority")
    draft_bindings = _read_object(draft_path, "draft-bindings")
    if "bindings" in draft_bindings and isinstance(draft_bindings.get("bindings"), dict):
        draft_bindings = relationship_binding_records(draft_bindings)

    (
        _auditor,
        resolve_workstation,
        workstation_truth_digest,
        _audit_context_digest,
        _finalizer,
    ) = _offline_full_plan_context(
        device_state_path,
        research_authority,
        relationship_binding_authority={},
    )
    evidence_sources = {
        "research_authority": args.research_evidence_source,
        "device_candidate": args.device_evidence_source,
        "workstation_truth": args.workstation_evidence_source,
    }
    manual_support_document: Optional[Dict[str, Any]] = None
    if args.manual_support_source:
        evidence_sources["manual_support"] = args.manual_support_source
        manual_support_path = Path(args.manual_support_source).expanduser()
        manual_support_document = _read_object(
            manual_support_path, "manual-support-source"
        )

    sealed = seal_relationship_binding_authority(
        candidate,
        research_authority,
        draft_bindings,
        authoring_mode=args.authoring_mode,
        automation_claim=args.authoring_mode == "automated_evidence_bound",
        evidence_sources=evidence_sources,
        workstation_truth_digest=workstation_truth_digest,
        resolve_workstation=resolve_workstation,
        manual_support_document=manual_support_document,
    )
    _atomic_write(output_path, sealed)
    print(
        json.dumps(
            {
                "output": str(output_path),
                "authoring_mode": sealed["authoring_mode"],
                "automation_claim": sealed["automation_claim"],
                "binding_record_count": len(sealed["bindings"]),
                "research_authority_sha256": sealed[
                    "research_authority_sha256"
                ],
                "candidate_sha256": sealed["candidate_sha256"],
                "workstation_truth_sha256": sealed[
                    "workstation_truth_sha256"
                ],
                "manual_support_document_sha256": sealed.get(
                    "manual_support_document_sha256"
                ),
                "manual_support_record_count": len(
                    sealed.get("manual_support_records") or {}
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
