"""Deployment-side issuance of independently reviewed PDF group decisions.

This program does not interpret chemistry. It prepares an exact work order
from an authenticated PDF, then requires a separate human review decision
before writing trusted group policy or a signed route signature. Signing a
decision authenticates the issuer's declaration; it is not execution approval.
The private key and reviewer decision file must live outside the repository
and the knowledge base. The planner never imports this module.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

from chem_agent_contracts.route_candidate import RouteGoalV1
from chem_agent_contracts.v2 import canonical_digest

from .route_attestation import verify_source_document_attestation
from .route_pdf_groups import enumerate_attested_pdf_experimental_groups
from .route_signature_review import (
    REVIEWED_ROUTE_SIGNATURE_MANIFEST_SCHEMA_VERSION_V1,
    SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1,
    ReviewedRouteSignatureScopeV1,
    signed_reviewed_route_signature_message_v1,
    verify_reviewed_route_signature_manifest,
)
from .route_signed_event import (
    TrustedIssuerPublicKeyV1, verify_signed_trusted_acquisition_event,
)


_ROUTE_ROLES = frozenset({"synthesis", "material_processing"})
_NONROUTE_ROLES = frozenset({
    "characterization", "testing", "performance_testing", "non_procedural",
})
_DECISION_FIELDS = frozenset({
    "schema_version", "work_order_digest", "reviewer", "review_method",
    "review_basis", "group_role", "required_capabilities", "route_signature",
    "chemical_review_completed", "execution_authorized",
})
_WORK_ORDER_FIELDS = frozenset({
    "schema_version", "status", "scope", "goal_id", "goal_digest",
    "source_event_digest", "blocks", "pending_checks",
    "human_chemical_review_completed", "execution_authorized",
})
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _read_object(path: str | Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("json_object_required")
    return value


def _outside_controlled_paths(path: str | Path, kb_root: str | Path) -> Path:
    result = Path(path).expanduser().resolve()
    kb = Path(kb_root).expanduser().resolve(strict=True)
    repository = Path(__file__).resolve().parent.parent
    if result.is_relative_to(kb) or result.is_relative_to(repository):
        raise ValueError("review_artifact_must_be_outside_kb_and_repository")
    return result


def _outside_kb(path: str | Path, kb_root: str | Path) -> Path:
    result = Path(path).expanduser().resolve()
    if result.is_relative_to(Path(kb_root).expanduser().resolve(strict=True)):
        raise ValueError("trust_config_must_be_outside_kb")
    return result


def _validated_trust(kb_root: str | Path, config_path: str | Path) -> dict[str, Any]:
    from .run_research_agent import load_route_trust_config

    config = _outside_kb(config_path, kb_root)
    return load_route_trust_config(str(config), str(kb_root))


def _expected_work_order(
    *, kb_root: str | Path, trust_config: str | Path,
    goal: RouteGoalV1, paper_id: str, experimental_group_id: str,
    source_digest: str,
) -> dict[str, Any]:
    trust = _validated_trust(kb_root, trust_config)
    enumeration = enumerate_attested_pdf_experimental_groups(
        kb_root, trust["signed_route_source_events"],
        trusted_public_keys=trust["trusted_route_public_keys"],
    )
    matches = [
        group for group in enumeration.groups
        if (group.source_scope.paper_id,
            group.source_scope.experimental_group_id,
            group.source_scope.source_digest)
        == (paper_id, experimental_group_id, source_digest)
    ]
    if len(matches) != 1:
        raise ValueError("signed_pdf_group_not_unique_or_not_enumerated")
    if any(issue.paper_id == paper_id for issue in enumeration.diagnostics):
        raise ValueError("signed_pdf_group_enumeration_incomplete")
    group = matches[0]
    source_matches: list[tuple[Any, Mapping[str, Any]]] = []
    for envelope in trust["signed_route_source_events"]:
        verdict = verify_signed_trusted_acquisition_event(
            envelope=envelope,
            trusted_public_keys=trust["trusted_route_public_keys"],
        )
        if (
            verdict.verified and verdict.event is not None
            and verdict.event.paper_id == paper_id
            and verdict.event.document_digest == source_digest
        ):
            source_matches.append((verdict.event, envelope))
    if len(source_matches) != 1:
        raise ValueError("signed_source_event_not_unique")
    event, envelope = source_matches[0]
    source = verify_source_document_attestation(
        kb_root=kb_root, trusted_event=event,
    )
    if not source.verified:
        raise ValueError("signed_source_attestation_unverified")
    source_doi = source.doi if source.document_kind == "primary_paper" else source.parent_doi
    target = goal.target
    scope = ReviewedRouteSignatureScopeV1(
        paper_id=paper_id,
        experimental_group_id=experimental_group_id,
        source_digest=source_digest,
        source_attestation_digest=event.attestation_digest,
        group_locator=group.source_scope.locator,
        section=group.source_scope.section,
        document_kind=source.document_kind,
        source_doi=source_doi,
        target_material=target.material,
        target_state=target.desired_state,
        target_objective=target.objective,
    )
    return {
        "schema_version": "signed_pdf_group_review_work_order_v1",
        "status": "pending_independent_chemical_review",
        "scope": scope.model_dump(mode="json"),
        "goal_id": goal.goal_id,
        "goal_digest": canonical_digest(goal),
        "source_event_digest": canonical_digest(envelope),
        "blocks": [
            {"locator": block.locator, "text": block.text}
            for block in group.blocks
        ],
        "pending_checks": [
            "experimental_group_boundary_and_role",
            "route_signature_chemical_interpretation",
            "required_capability_completeness_if_route",
            "cross_group_parameter_isolation",
        ],
        "human_chemical_review_completed": False,
        "execution_authorized": False,
    }


def prepare_group_review_work_order(
    *, kb_root: str | Path, trust_config: str | Path,
    goal: RouteGoalV1, paper_id: str, experimental_group_id: str,
    source_digest: str,
) -> dict[str, Any]:
    """Produce a review request, never an approval or a trusted role."""
    if not isinstance(goal, RouteGoalV1):
        raise ValueError("trusted_route_goal_required")
    return _expected_work_order(
        kb_root=kb_root, trust_config=trust_config, goal=goal,
        paper_id=paper_id, experimental_group_id=experimental_group_id,
        source_digest=source_digest,
    )


def list_attested_review_groups(
    *, kb_root: str | Path, trust_config: str | Path,
) -> dict[str, Any]:
    """Expose exact signed-PDF scopes for independent review triage."""
    trust = _validated_trust(kb_root, trust_config)
    inventory = enumerate_attested_pdf_experimental_groups(
        kb_root, trust["signed_route_source_events"],
        trusted_public_keys=trust["trusted_route_public_keys"],
    )
    return {
        "schema_version": "signed_pdf_group_review_inventory_v1",
        "verification_mode": "authenticated_source_group_enumeration",
        "group_scopes": [group.source_scope.model_dump(mode="json")
                         for group in inventory.groups],
        "diagnostics": [vars(item) for item in inventory.diagnostics],
        "human_chemical_review_completed": False,
        "execution_authorized": False,
    }


def _validated_decision(
    decision: Mapping[str, Any], *, work_order: Mapping[str, Any],
) -> tuple[str, list[str], dict[str, Any] | None]:
    if not isinstance(decision, Mapping) or set(decision) != _DECISION_FIELDS:
        raise ValueError("review_decision_schema_invalid")
    if decision["schema_version"] != "independent_group_review_decision_v1":
        raise ValueError("review_decision_schema_invalid")
    if decision["work_order_digest"] != canonical_digest(work_order):
        raise ValueError("review_decision_work_order_mismatch")
    reviewer = decision["reviewer"]
    if (
        not isinstance(reviewer, str) or not reviewer.strip()
        or reviewer != reviewer.strip() or len(reviewer) > 128
        or any(ord(character) < 32 for character in reviewer)
    ):
        raise ValueError("reviewer_identity_invalid")
    if decision["review_method"] != "independent_human_pdf_review":
        raise ValueError("unsupported_chemical_review_method")
    basis = decision["review_basis"]
    if (
        not isinstance(basis, str) or not basis.strip()
        or basis != basis.strip() or len(basis) > 4000
    ):
        raise ValueError("review_basis_missing")
    if decision["chemical_review_completed"] is not True:
        raise ValueError("chemical_review_not_completed")
    if decision["execution_authorized"] is not False:
        raise ValueError("review_cannot_authorize_execution")
    role = decision["group_role"]
    if role not in _ROUTE_ROLES | _NONROUTE_ROLES:
        raise ValueError("reviewed_group_role_invalid")
    capabilities = decision["required_capabilities"]
    if not isinstance(capabilities, list) or any(
        not isinstance(item, str) or not item.strip() or item != item.strip()
        for item in capabilities
    ) or len(capabilities) != len(set(capabilities)):
        raise ValueError("reviewed_capabilities_invalid")
    signature = decision["route_signature"]
    if role in _ROUTE_ROLES:
        if not capabilities:
            raise ValueError("route_capability_review_incomplete")
        if not isinstance(signature, dict):
            raise ValueError("route_signature_review_missing")
    elif capabilities or signature is not None:
        raise ValueError("nonroute_must_not_claim_route_signature_or_capabilities")
    return role, capabilities, signature


def issue_group_review(
    *, kb_root: str | Path, trust_config: str | Path,
    goal: RouteGoalV1, work_order: Mapping[str, Any],
    decision: Mapping[str, Any], private_key_path: str | Path,
    decision_path: str | Path, issuer: str, key_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return validated config and receipt; caller persists them atomically."""
    if not isinstance(work_order, Mapping) or set(work_order) != _WORK_ORDER_FIELDS:
        raise ValueError("review_work_order_invalid")
    if work_order.get("schema_version") != "signed_pdf_group_review_work_order_v1":
        raise ValueError("review_work_order_invalid")
    if not isinstance(work_order.get("scope"), Mapping):
        raise ValueError("review_work_order_scope_invalid")
    supplied = work_order["scope"]
    try:
        scope = ReviewedRouteSignatureScopeV1.model_validate(supplied, strict=True)
    except Exception as exc:
        raise ValueError("review_work_order_scope_invalid") from exc
    expected = _expected_work_order(
        kb_root=kb_root, trust_config=trust_config, goal=goal,
        paper_id=scope.paper_id,
        experimental_group_id=scope.experimental_group_id,
        source_digest=scope.source_digest,
    )
    if dict(work_order) != expected:
        raise ValueError("review_work_order_stale_or_untrusted")
    role, capabilities, signature = _validated_decision(decision, work_order=expected)
    if not isinstance(issuer, str) or not issuer.strip() or issuer != issuer.strip():
        raise ValueError("review_issuer_invalid")
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        raise ValueError("review_key_id_invalid")
    external_decision = _outside_controlled_paths(
        decision_path, kb_root,
    ).resolve(strict=True)
    if _read_object(external_decision) != dict(decision):
        raise ValueError("review_decision_artifact_mismatch")
    key_path = _outside_controlled_paths(private_key_path, kb_root).resolve(strict=True)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = serialization.load_pem_private_key(
        key_path.read_bytes(), password=None,
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("review_key_must_be_ed25519")
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    config_path = _outside_kb(trust_config, kb_root)
    config = _read_object(config_path)
    trusted = _validated_trust(kb_root, config_path)
    if any(
        source_key.allowed_issuer == issuer
        or source_key.public_key_bytes == public_bytes
        for source_key in trusted["trusted_route_public_keys"].values()
    ):
        raise ValueError("route_reviewer_must_be_independent_of_source_issuer")
    keys = config["trusted_route_signature_public_keys"]
    public_record = {
        "public_key_base64": base64.b64encode(public_bytes).decode("ascii"),
        "allowed_issuer": issuer,
    }
    if key_id in keys and keys[key_id] != public_record:
        raise ValueError("review_key_id_conflict")
    if key_id in config["trusted_route_public_keys"]:
        raise ValueError("review_key_id_conflicts_with_source_key")
    group_key = {
        "paper_id": scope.paper_id,
        "experimental_group_id": scope.experimental_group_id,
        "source_digest": scope.source_digest,
    }
    role_record = {**group_key, "group_role": role}
    capability_record = {**group_key, "required_capabilities": capabilities}
    if any(
        all(item.get(k) == v for k, v in group_key.items())
        for item in config["trusted_route_group_roles_by_group"]
    ):
        raise ValueError("group_role_already_reviewed")
    if any(
        all(item.get(k) == v for k, v in group_key.items())
        for item in config["trusted_route_capabilities_by_group"]
    ):
        raise ValueError("group_capabilities_already_reviewed")
    envelope: dict[str, Any] | None = None
    if role in _ROUTE_ROLES:
        decision_digest = canonical_digest(decision)
        manifest = {
            "schema_version": REVIEWED_ROUTE_SIGNATURE_MANIFEST_SCHEMA_VERSION_V1,
            **expected["scope"],
            "issuer": issuer,
            "review_basis": (
                f"independent_human_pdf_review; reviewer={decision['reviewer']}; "
                f"decision={decision_digest}; basis={decision['review_basis']}"
            ),
            "route_signature": signature,
        }
        envelope = {
            "schema_version": SIGNED_REVIEWED_ROUTE_SIGNATURE_SCHEMA_VERSION_V1,
            "key_id": key_id,
            "manifest": manifest,
            "signature": base64.b64encode(private_key.sign(
                signed_reviewed_route_signature_message_v1(
                    key_id=key_id, manifest=manifest,
                )
            )).decode("ascii"),
        }
        verdict = verify_reviewed_route_signature_manifest(
            envelope=envelope,
            trusted_public_keys={key_id: TrustedIssuerPublicKeyV1(
                public_key_bytes=public_bytes, allowed_issuer=issuer,
            )},
            expected_scope=scope,
        )
        if not verdict.verified:
            raise ValueError(f"signed_route_review_invalid:{verdict.reason_code}")
        review_scope = (
            scope.paper_id, scope.experimental_group_id, scope.source_digest,
            scope.target_material, scope.target_state, scope.target_objective,
        )
        if any((
            item.get("manifest", {}).get("paper_id"),
            item.get("manifest", {}).get("experimental_group_id"),
            item.get("manifest", {}).get("source_digest"),
            item.get("manifest", {}).get("target_material"),
            item.get("manifest", {}).get("target_state"),
            item.get("manifest", {}).get("target_objective"),
        ) == review_scope for item in config["signed_route_signature_reviews"]):
            raise ValueError("route_signature_already_reviewed")
    # The v1 runtime config pairs review keys with route-signature envelopes.
    # A nonroute role declaration belongs to deployment-controlled group
    # policy, and must not install an otherwise orphaned signature key.
    if role in _ROUTE_ROLES:
        keys[key_id] = public_record
    config["trusted_route_group_roles_by_group"].append(role_record)
    if role in _ROUTE_ROLES:
        config["trusted_route_capabilities_by_group"].append(capability_record)
        assert envelope is not None
        config["signed_route_signature_reviews"].append(envelope)
    receipt = {
        "schema_version": "issued_independent_group_review_receipt_v1",
        "verification_mode": "independent_human_pdf_review",
        "work_order_digest": canonical_digest(expected),
        "decision_digest": canonical_digest(decision),
        "scope": expected["scope"],
        "group_role": role,
        "required_capabilities": capabilities,
        "route_review_envelope": envelope,
        "issuer": issuer,
        "key_id": key_id,
        "public_key_base64": public_record["public_key_base64"],
        "human_chemical_review_completed": True,
        "execution_authorized": False,
    }
    receipt["signature"] = base64.b64encode(private_key.sign(
        b"chem-agent.issued-group-review-receipt.v1\x00" +
        json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )).decode("ascii")
    return config, receipt


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    from .route_receipt_producer import _atomic_replace

    _atomic_replace(path, json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ).encode("utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list-groups")
    prepare = commands.add_parser("prepare-work-order")
    issue = commands.add_parser("issue-review")
    for command in (prepare, issue):
        command.add_argument("--kb-root", required=True)
        command.add_argument("--route-trust-config", required=True)
        command.add_argument("--goal-json", required=True)
    listing.add_argument("--kb-root", required=True)
    listing.add_argument("--route-trust-config", required=True)
    listing.add_argument("--inventory-out", required=True)
    for name in ("paper-id", "experimental-group-id", "source-digest", "work-order-out"):
        prepare.add_argument("--" + name, required=True)
    for name in ("work-order", "decision", "private-key", "issuer", "key-id", "receipt-out"):
        issue.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "list-groups":
            inventory = list_attested_review_groups(
                kb_root=args.kb_root, trust_config=args.route_trust_config,
            )
            _atomic_json(Path(args.inventory_out), inventory)
            print(json.dumps({"status": "review_inventory_ready",
                              "group_count": len(inventory["group_scopes"]),
                              "diagnostic_count": len(inventory["diagnostics"]),
                              "inventory": args.inventory_out}))
        elif args.command == "prepare-work-order":
            goal = RouteGoalV1.model_validate(_read_object(args.goal_json), strict=True)
            order = prepare_group_review_work_order(
                kb_root=args.kb_root, trust_config=args.route_trust_config,
                goal=goal, paper_id=args.paper_id,
                experimental_group_id=args.experimental_group_id,
                source_digest=args.source_digest,
            )
            _atomic_json(Path(args.work_order_out), order)
            print(json.dumps({"status": "pending_independent_chemical_review",
                              "work_order": args.work_order_out}))
        else:
            goal = RouteGoalV1.model_validate(_read_object(args.goal_json), strict=True)
            decision_path = _outside_controlled_paths(args.decision, args.kb_root)
            order = _read_object(args.work_order)
            decision = _read_object(decision_path)
            config_path = _outside_kb(args.route_trust_config, args.kb_root)
            original_config = config_path.read_bytes()
            config, receipt = issue_group_review(
                kb_root=args.kb_root, trust_config=args.route_trust_config,
                goal=goal, work_order=order, decision=decision,
                private_key_path=args.private_key, decision_path=decision_path,
                issuer=args.issuer, key_id=args.key_id,
            )
            # Validate a temporary complete config before replacing the active one.
            descriptor, name = tempfile.mkstemp(
                prefix=".route-review-", suffix=".json", dir=config_path.parent,
            )
            os.close(descriptor)
            staged = Path(name)
            try:
                _atomic_json(staged, config)
                _validated_trust(args.kb_root, staged)
                if config_path.read_bytes() != original_config:
                    raise ValueError("trust_config_changed_during_review_issuance")
                _atomic_json(Path(args.receipt_out), receipt)
                staged.replace(config_path)
            finally:
                staged.unlink(missing_ok=True)
            print(json.dumps({"status": "issued_independent_group_review",
                              "group_role": receipt["group_role"],
                              "route_review_signed": bool(receipt["route_review_envelope"]),
                              "execution_authorized": False}))
    except (OSError, ValueError, TypeError, SystemExit) as exc:
        print(json.dumps({"status": "blocked", "reason_code": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
