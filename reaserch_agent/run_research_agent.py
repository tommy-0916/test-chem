#!/usr/bin/env python3
"""CLI entrypoint for running the research agent bootstrap flow."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reaserch_agent import ResearchAgent
from reaserch_agent.plan_ledger import (
    PlanLedger,
    default_ledger_path,
    generate_campaign_id,
)
from reaserch_agent.tools import load_device_context
from reaserch_agent.tools.device_context import default_workstations_dir
from reaserch_agent.tools.ingestion import KnowledgeIngestion, classify_reference
from reaserch_agent.utils.llm_factory import LLMFactory
from reaserch_agent.route_signed_event import (
    TrustedIssuerPublicKeyV1,
    verify_signed_trusted_acquisition_event,
)
from reaserch_agent.route_signature_review import (
    ReviewedRouteSignatureScopeV1,
    verify_reviewed_route_signature_manifest,
)


DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"
DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parent / "chem_kb"
DEFAULT_DEVICE_WORKSTATIONS_DIR = default_workstations_dir()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the research agent. Bootstrap and new observation events "
            "are implemented end-to-end."
        )
    )
    parser.add_argument(
        "--event-type",
        default="bootstrap",
        help="Event type to run. Default: bootstrap",
    )
    parser.add_argument(
        "--contract-version",
        choices=["v1", "v2"],
        default="v2",
        help="Internal agent contract. Default: v2; use v1 for compatibility rollback.",
    )
    parser.add_argument(
        "--query",
        help="Human query for the research agent. If omitted, the script will ask interactively.",
    )
    parser.add_argument(
        "--constraints-json",
        default="{}",
        help='JSON object passed as event constraints, e.g. \'{"目标":"先生成首轮macro plan"}\'',
    )
    parser.add_argument(
        "--route-trust-config",
        help=(
            "Independent deployment JSON containing signed PDF acquisition events, "
            "issuer-scoped public keys, signed route reviews, group policy, and "
            "optional trusted candidate supply-spec file paths. "
            "Never read route trust from event constraints or a saved state."
        ),
    )
    parser.add_argument(
        "--payload-json",
        default="{}",
        help='JSON object passed as event payload.',
    )
    parser.add_argument(
        "--previous-state",
        help="Path to a saved state JSON used as context for new observation events.",
    )
    parser.add_argument(
        "--observation",
        help=(
            "Convenience text observation. For new observation events, this is stored "
            "as payload.observation.summary unless payload-json already contains one."
        ),
    )
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        help=(
            "Reference material for the campaign: a local PDF/JSON/TXT/MD path, a DOI, "
            "an arXiv id, or a paper title. Can be repeated. Local files are ingested "
            "into the knowledge base immediately; DOI/arXiv/title entries are recorded "
            "for online resolution."
        ),
    )
    parser.add_argument(
        "--campaign-id",
        help=(
            "Campaign identifier grouping this run's plans, literature, and memory. "
            "Bootstrap runs auto-generate one when omitted; observation runs inherit "
            "it from --previous-state."
        ),
    )
    parser.add_argument(
        "--ledger-path",
        help=(
            "Path of the plan-version JSONL ledger. Defaults to "
            "campaigns/<campaign_id>/plan_versions.jsonl at the repo root."
        ),
    )
    parser.add_argument(
        "--no-ledger",
        action="store_true",
        help="Do not append this run's plan event to the plan-version ledger file.",
    )
    parser.add_argument(
        "--knowledge-base-dir",
        help="Directory containing knowledge-base PDF or JSON files. Defaults to reaserch_agent/chem_kb.",
    )
    parser.add_argument(
        "--device-workstations-dir",
        help=(
            "Optional workstation description directory passed to B1 as device_context. "
            f"Default when --include-device-context is used: {DEFAULT_DEVICE_WORKSTATIONS_DIR}."
        ),
    )
    parser.add_argument(
        "--device-context-json",
        help="Optional JSON object/string for device_context; overrides --device-workstations-dir.",
    )
    parser.add_argument(
        "--include-device-context",
        action="store_true",
        help=(
            "Load the compact workstation capability index into B1 constraints "
            "before the first LLM call. Enabled by default."
        ),
    )
    parser.add_argument(
        "--device-status-json",
        help=(
            "Optional JSON file with live station availability. Unavailable stations "
            "are flagged inside device_context so research planning avoids them."
        ),
    )
    parser.add_argument(
        "--memory-dir",
        help="Optional directory for memory retrieval. Defaults to the knowledge base directory.",
    )
    parser.add_argument(
        "--enable-memory",
        action="store_true",
        help="Enable memory retrieval. By default memory is disabled.",
    )
    parser.add_argument(
        "--online-literature",
        action="store_true",
        help=(
            "Always run online literature acquisition (seed resolution + citation "
            "snowball + keyword search) before planning. Enabled by default."
        ),
    )
    parser.add_argument(
        "--no-online-literature",
        action="store_true",
        help="Never contact external scholarly APIs during this run.",
    )
    parser.add_argument(
        "--download-pdfs",
        action="store_true",
        help="Download open-access PDFs during literature acquisition.",
    )
    parser.add_argument(
        "--web-search",
        action="store_true",
        help=(
            "Enable the open-web line during literature acquisition: search "
            "(Tavily/Serper/Brave/SearXNG/DuckDuckGo, auto by configured keys), "
            "read top pages via Jina Reader, and archive them as web_unverified "
            "leads. Enabled by default."
        ),
    )
    parser.add_argument(
        "--no-web-search",
        action="store_true",
        help="Force-disable the open-web line (overrides RESEARCH_WEB_SEARCH).",
    )
    parser.add_argument(
        "--model-name",
        help="Optional model name. If provided, it will override REFINER_LLM_MODEL_NAME.",
    )
    parser.add_argument(
        "--api-key",
        help="Optional API key. If provided, it will override REFINER_LLM_API_KEY.",
    )
    parser.add_argument(
        "--base-url",
        help="Optional model endpoint URL. If provided, it will override REFINER_LLM_ENDPOINT_URL.",
    )
    parser.add_argument(
        "--wire-api",
        choices=["chat", "codex_responses"],
        default="chat",
        help=(
            "LLM wire API. Use codex_responses for the gpt-5.5 Codex-compatible endpoint."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        default="xhigh",
        help="Reasoning effort used when --wire-api codex_responses. Default: xhigh.",
    )
    parser.add_argument(
        "--llm-timeout-seconds",
        type=float,
        default=240.0,
        help="LLM timeout seconds. Default: 240.",
    )
    parser.add_argument(
        "--llm-max-retries",
        type=int,
        default=8,
        help="Maximum LLM retries for the Research Agent and SDK transport. Default: 8.",
    )
    parser.add_argument(
        "--disable-llm",
        action="store_true",
        help="Force heuristic mode even if model credentials are configured.",
    )
    parser.add_argument(
        "--max-survey-rounds",
        type=int,
        default=2,
        help="Maximum number of B1 survey rounds. Default: 2",
    )
    parser.add_argument(
        "--knowledge-top-k",
        type=int,
        default=5,
        help="Number of knowledge hits to keep per round. Default: 5",
    )
    parser.add_argument(
        "--memory-top-k",
        type=int,
        default=3,
        help="Number of memory hits to keep. Default: 3",
    )
    parser.add_argument(
        "--print-state-json",
        action="store_true",
        help="Print the full returned state as JSON.",
    )
    parser.add_argument(
        "--save-state",
        help="Optional path for saving the final state JSON.",
    )
    parser.set_defaults(
        include_device_context=True,
        online_literature=True,
        web_search=True,
    )
    return parser


def parse_json_dict(raw_text: str, label: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} 不是合法 JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SystemExit(f"{label} 必须是 JSON object。")
    return parsed


def load_route_trust_config(
    path_text: str | None, knowledge_base_dir: str | None,
) -> Dict[str, Any]:
    """Load deployment-owned route trust separately from workflow input.

    Signature checks here catch bad configuration early. The route pipeline
    still verifies each signature and compares each review to the independently
    enumerated PDF group and requested target before selecting a route.
    """

    if not path_text:
        return {}

    from reaserch_agent.tools.literature_acquisition import default_kb_dir

    path = Path(path_text).expanduser().resolve()
    kb_root = Path(knowledge_base_dir).expanduser().resolve() if knowledge_base_dir else default_kb_dir().resolve()
    if path.is_relative_to(kb_root):
        raise SystemExit("route-trust-config must be outside the knowledge base")
    if not path.is_file():
        raise SystemExit(f"route-trust-config must be an existing JSON file: {path}")

    def unique_object(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"invalid route-trust-config: {exc}") from exc
    fields = {
        "schema_version", "signed_route_source_events", "trusted_route_public_keys",
        "signed_route_signature_reviews", "trusted_route_signature_public_keys",
        "trusted_route_capabilities_by_group", "trusted_route_group_roles_by_group",
    }
    optional_fields = {"trusted_inventory_register_paths"}
    if (not isinstance(raw, dict) or not fields.issubset(raw)
            or not set(raw).issubset(fields | optional_fields)
            or raw["schema_version"] != "route-trust-config/v1"):
        raise SystemExit("route-trust-config must contain the required v1 trust fields "
                         "and only recognized optional fields")

    inventory_paths: Dict[str, str] = {}
    if "trusted_inventory_register_paths" in raw:
        registers = raw["trusted_inventory_register_paths"]
        if (not isinstance(registers, dict)
                or not set(registers).issubset({"candidate_supply_spec/v1"})):
            raise SystemExit("trusted_inventory_register_paths must be an object "
                             "containing only candidate_supply_spec/v1")
        for register, name in registers.items():
            if (not isinstance(name, str) or not name.strip()
                    or name != name.strip() or name.startswith(("\\\\", "//"))
                    or re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", name)):
                raise SystemExit("trusted_inventory_register_paths requires non-empty local file paths")
            try:
                registered = Path(name).expanduser()
                if not registered.is_absolute():
                    registered = path.parent / registered
                registered = registered.resolve(strict=True)
                if not registered.is_file() or not registered.is_relative_to(kb_root):
                    raise ValueError("registered file is outside the knowledge base or not a file")
            except (OSError, RuntimeError, ValueError) as exc:
                raise SystemExit("trusted_inventory_register_paths requires an existing "
                                 "file inside the knowledge base") from exc
            inventory_paths[register] = str(registered)

    def parse_keys(value: Any, label: str) -> Dict[str, TrustedIssuerPublicKeyV1]:
        if not isinstance(value, dict):
            raise SystemExit(f"{label} must be an object")
        parsed: Dict[str, TrustedIssuerPublicKeyV1] = {}
        for key_id, record in value.items():
            if not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", key_id):
                raise SystemExit(f"{label} contains an invalid key ID")
            if not isinstance(record, dict) or set(record) != {"public_key_base64", "allowed_issuer"}:
                raise SystemExit(f"{label}.{key_id} must contain public_key_base64 and allowed_issuer")
            encoded, issuer = record["public_key_base64"], record["allowed_issuer"]
            if not isinstance(encoded, str) or not isinstance(issuer, str) or not issuer.strip() or issuer != issuer.strip():
                raise SystemExit(f"{label}.{key_id} has invalid key or issuer")
            try:
                key_bytes = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise SystemExit(f"{label}.{key_id} has invalid base64") from exc
            if len(key_bytes) != 32 or base64.b64encode(key_bytes).decode("ascii") != encoded:
                raise SystemExit(f"{label}.{key_id} must be a canonical 32-byte Ed25519 public key")
            parsed[key_id] = TrustedIssuerPublicKeyV1(key_bytes, issuer)
        return parsed

    source_keys = parse_keys(raw["trusted_route_public_keys"], "trusted_route_public_keys")
    review_keys = parse_keys(
        raw["trusted_route_signature_public_keys"],
        "trusted_route_signature_public_keys",
    )
    sources = raw["signed_route_source_events"]
    reviews = raw["signed_route_signature_reviews"]
    if not isinstance(sources, list) or not sources or not source_keys:
        raise SystemExit("route-trust-config requires signed source events and source public keys")
    if not isinstance(reviews, list) or bool(reviews) != bool(review_keys):
        raise SystemExit("signed route reviews and review public keys must be supplied together")
    source_scopes: set[tuple[str, str, str]] = set()
    source_identities: set[tuple[str, str]] = set()
    for index, envelope in enumerate(sources):
        verdict = verify_signed_trusted_acquisition_event(
            envelope=envelope, trusted_public_keys=source_keys,
        )
        if not verdict.verified or verdict.event is None:
            raise SystemExit(f"signed_route_source_events[{index}] invalid: {verdict.reason_code}")
        event = verdict.event
        identity = (event.paper_id, event.document_digest)
        if identity in source_identities:
            raise SystemExit(f"duplicate signed source identity at index {index}")
        source_identities.add(identity)
        resolved_pdf = (kb_root / event.kb_relative_path).resolve()
        if not resolved_pdf.is_relative_to(kb_root) or not resolved_pdf.is_file():
            raise SystemExit(f"signed_route_source_events[{index}] PDF path is missing or outside the KB")
        source_scopes.add((event.paper_id, event.document_digest, event.attestation_digest))

    review_scopes: set[tuple[str, str, str, str, str, str]] = set()
    for index, envelope in enumerate(reviews):
        manifest = envelope.get("manifest") if isinstance(envelope, dict) else None
        try:
            expected = ReviewedRouteSignatureScopeV1.model_validate(
                {name: manifest[name] for name in ReviewedRouteSignatureScopeV1.model_fields},
                strict=True,
            )
        except (TypeError, KeyError, ValueError) as exc:
            raise SystemExit(f"signed_route_signature_reviews[{index}] has invalid scope") from exc
        verdict = verify_reviewed_route_signature_manifest(
            envelope=envelope, trusted_public_keys=review_keys, expected_scope=expected,
        )
        if not verdict.verified or verdict.receipt is None:
            raise SystemExit(f"signed_route_signature_reviews[{index}] invalid: {verdict.reason_code}")
        review = verdict.receipt.manifest
        if (review.paper_id, review.source_digest, review.source_attestation_digest) not in source_scopes:
            raise SystemExit(f"signed_route_signature_reviews[{index}] has no signed source")
        scope = (
            review.paper_id, review.experimental_group_id, review.source_digest,
            review.target_material, review.target_state, review.target_objective,
        )
        if scope in review_scopes:
            raise SystemExit(f"duplicate signed route review at index {index}")
        review_scopes.add(scope)

    digest_pattern = re.compile(r"sha256_[0-9a-f]{64}\Z")
    def parse_groups(value: Any, field: str, leaf: str) -> Dict[tuple[str, str, str], Any]:
        if not isinstance(value, list):
            raise SystemExit(f"{field} must be an array")
        parsed: Dict[tuple[str, str, str], Any] = {}
        for index, record in enumerate(value):
            if not isinstance(record, dict) or set(record) != {"paper_id", "experimental_group_id", "source_digest", leaf}:
                raise SystemExit(f"{field}[{index}] has invalid fields")
            group = tuple(record[name] for name in ("paper_id", "experimental_group_id", "source_digest"))
            if any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in group) or not digest_pattern.fullmatch(group[2]):
                raise SystemExit(f"{field}[{index}] has invalid group identity")
            if group in parsed:
                raise SystemExit(f"{field}[{index}] repeats a group identity")
            if not any(paper == group[0] and digest == group[2] for paper, digest, _ in source_scopes):
                raise SystemExit(f"{field}[{index}] has no signed source")
            parsed[group] = record[leaf]
        return parsed

    capabilities = parse_groups(
        raw["trusted_route_capabilities_by_group"],
        "trusted_route_capabilities_by_group", "required_capabilities",
    )
    roles = parse_groups(
        raw["trusted_route_group_roles_by_group"],
        "trusted_route_group_roles_by_group", "group_role",
    )
    for group, values in capabilities.items():
        if (not isinstance(values, list) or not values
                or any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in values)
                or len(set(values)) != len(values)):
            raise SystemExit(f"trusted_route_capabilities_by_group has invalid capabilities for {group}")
        if group not in roles:
            raise SystemExit(f"trusted_route_capabilities_by_group lacks a role for {group}")
    for group, role in roles.items():
        if not isinstance(role, str) or role not in {
            "synthesis", "material_processing", "characterization", "testing",
            "performance_testing", "non_procedural",
        }:
            raise SystemExit(f"trusted_route_group_roles_by_group has invalid role for {group}")

    result = {
        "signed_route_source_events": sources,
        "trusted_route_public_keys": source_keys,
        "signed_route_signature_reviews": reviews,
        "trusted_route_signature_public_keys": review_keys,
        "trusted_route_capabilities_by_group": capabilities,
        "trusted_route_group_roles_by_group": roles,
    }
    if "trusted_inventory_register_paths" in raw:
        result["trusted_inventory_register_paths"] = inventory_paths
    return result


def attach_device_context(args: argparse.Namespace, constraints: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(constraints or {})
    if args.device_status_json:
        updated["device_status_path"] = str(
            Path(args.device_status_json).expanduser().resolve()
        )
    if args.device_context_json:
        parsed = parse_json_dict(args.device_context_json, "device_context_json")
        updated["device_context"] = parsed
        return updated

    if args.include_device_context or args.device_workstations_dir:
        workstations_dir = (
            Path(args.device_workstations_dir).expanduser().resolve()
            if args.device_workstations_dir
            else DEFAULT_DEVICE_WORKSTATIONS_DIR
        )
        updated["device_workstations_dir"] = str(workstations_dir)
        updated["device_context"] = load_device_context(workstations_dir)

    return updated


def resolve_reference_inputs(
    references: list[str],
    knowledge_base_dir: str | None,
) -> list[Dict[str, Any]]:
    """Classify --reference entries and ingest local files into the KB now.

    DOI / arXiv / title references are recorded with status pending_resolution;
    the online ingestion layer resolves them into papers.
    """
    if not references:
        return []

    kb_dir = (
        Path(knowledge_base_dir).expanduser().resolve()
        if knowledge_base_dir
        else DEFAULT_KNOWLEDGE_DIR
    )
    ingestion: KnowledgeIngestion | None = None
    entries: list[Dict[str, Any]] = []
    for raw in references:
        entry = classify_reference(raw)
        if entry.get("kind") in {"local_file", "local_dir"}:
            try:
                if ingestion is None:
                    ingestion = KnowledgeIngestion(kb_dir)
                written = ingestion.ingest_path(
                    entry["path"],
                    recursive=entry["kind"] == "local_dir",
                )
                entry["status"] = "ingested"
                entry["written_records"] = [str(path) for path in written]
            except Exception as exc:
                entry["status"] = "ingest_failed"
                entry["error"] = str(exc)
        elif entry.get("kind") == "empty":
            entry["status"] = "skipped"
        else:
            entry["status"] = "pending_resolution"
        entries.append(entry)
    return entries


def resolve_campaign_id(
    args: argparse.Namespace,
    query: str,
    previous_state: Dict[str, Any] | None,
) -> str:
    campaign_id = (args.campaign_id or "").strip()
    if campaign_id:
        return campaign_id
    if previous_state:
        inherited = str(previous_state.get("campaign_id", "") or "").strip()
        if inherited:
            return inherited
    normalized_event = re.sub(r"[\s\-]+", "_", args.event_type.strip().lower())
    if normalized_event == "bootstrap":
        return generate_campaign_id(query)
    return ""


def append_plan_ledger(args: argparse.Namespace, state: Any) -> Path | None:
    """Persist this run's plan event (one run = at most one plan event)."""
    if args.no_ledger or not state.campaign_id or not state.plan_revisions:
        return None
    ledger_path = (
        Path(args.ledger_path).expanduser().resolve()
        if args.ledger_path
        else default_ledger_path(state.campaign_id)
    )
    record = state.plan_revisions[-1]
    PlanLedger(ledger_path).append(record)
    print(
        f"plan_ledger: {ledger_path} "
        f"(v{record.get('plan_version')} {record.get('event')}/{record.get('scope')})"
    )
    return ledger_path


def configure_model_env(args: argparse.Namespace) -> None:
    if args.model_name:
        os.environ["REFINER_LLM_MODEL_NAME"] = args.model_name
    if args.api_key:
        os.environ["REFINER_LLM_API_KEY"] = args.api_key
        os.environ["OPENAI_API_KEY"] = args.api_key
    if args.base_url:
        os.environ["REFINER_LLM_ENDPOINT_URL"] = args.base_url
    os.environ["REFINER_LLM_TIMEOUT_SECONDS"] = str(args.llm_timeout_seconds)
    os.environ["REFINER_LLM_MAX_RETRIES"] = str(max(0, args.llm_max_retries))
    if args.wire_api == "codex_responses":
        os.environ["REFINER_LLM_WIRE_API"] = "codex_responses"
        os.environ["REFINER_LLM_REASONING_EFFORT"] = args.reasoning_effort
    else:
        os.environ.pop("REFINER_LLM_WIRE_API", None)


def get_query(args: argparse.Namespace) -> str:
    if args.query and args.query.strip():
        return args.query.strip()

    normalized_event_type = re.sub(r"[\s\-]+", "_", args.event_type.strip().lower())
    if args.previous_state and normalized_event_type in {
        "new_observation",
        "post_observation",
        "observation",
        "observation_returned",
    }:
        return ""

    query = input("请输入 bootstrap query: ").strip()
    if not query:
        raise SystemExit("query 不能为空。")
    return query


def print_summary(state: Any) -> None:
    print(f"status: {state.status}")
    print(f"current_branch: {state.current_branch}")
    print(f"next_branch: {state.next_branch}")
    print(f"stage_route: {state.stage_route}")
    print(f"current_stage: {state.current_stage}")
    print(f"current_stage_plan: {state.current_stage_plan}")

    if state.knowledge_hits:
        print(f"top_knowledge_hit: {state.knowledge_hits[0].title}")

    print("macro_plan:")
    if not state.macro_plan:
        print("  (empty)")
        return

    for step in state.macro_plan:
        step_no = step.get("步骤序号", "?")
        operation = step.get("操作", "")
        target = step.get("试剂/对象", "")
        parameters = step.get("参数", "")
        print(f"  {step_no}. {operation}")
        print(f"     试剂/对象: {target}")
        print(f"     参数: {parameters}")


def slugify_query(query: str, max_len: int = 48) -> str:
    normalized = re.sub(r"\s+", "_", query.strip())
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_\-]+", "", normalized)
    normalized = normalized.strip("_-")
    return (normalized[:max_len] or "query").strip("_-")


def build_state_json(state: Any) -> str:
    if hasattr(state, "debug_snapshot"):
        payload = state.debug_snapshot()
    else:
        payload = state.to_dict()
    return json.dumps(payload, ensure_ascii=False, indent=2)


def load_previous_state(path_text: str | None) -> Dict[str, Any] | None:
    if not path_text:
        return None
    path = Path(path_text).expanduser().resolve()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"previous-state 不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("previous-state 必须是 JSON object。")
    return parsed


def write_debug_log(state: Any, log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    created_at = getattr(state, "created_at", "") or ""
    timestamp = created_at.replace(":", "-").replace("T", "_").split(".", 1)[0]
    filename = f"{timestamp}_{slugify_query(state.event.query)}.json"
    output_path = log_dir / filename
    output_path.write_text(build_state_json(state), encoding="utf-8")
    return output_path


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    os.environ["CHEM_LLM_COMPONENT"] = "research"
    configure_model_env(args)
    query = get_query(args)
    constraints = attach_device_context(
        args,
        parse_json_dict(args.constraints_json, "constraints_json"),
    )
    if args.route_trust_config and args.contract_version != "v2":
        raise SystemExit("--route-trust-config requires --contract-version v2")
    route_trust = load_route_trust_config(
        args.route_trust_config, args.knowledge_base_dir,
    )
    payload = parse_json_dict(args.payload_json, "payload_json")
    if args.observation and "observation" not in payload:
        payload["observation"] = {"summary": args.observation.strip()}
    previous_state = load_previous_state(args.previous_state)
    campaign_id = resolve_campaign_id(args, query, previous_state)
    reference_inputs = resolve_reference_inputs(args.reference, args.knowledge_base_dir)

    print(
        "starting research agent: "
        f"event_type={args.event_type}, "
        f"model={args.model_name or 'env/default'}, "
        f"wire_api={args.wire_api}, "
        f"device_context={'on' if constraints.get('device_context') else 'off'}, "
        f"memory={'on' if args.enable_memory else 'off'}, "
        f"campaign={campaign_id or 'off'}, "
        f"references={len(reference_inputs)}",
        flush=True,
    )
    if args.wire_api == "codex_responses":
        from agent_skills.responses_stream import configured_responses_streaming

        transport = os.getenv("REFINER_RESPONSES_TRANSPORT", "direct").strip().lower()
        response_mode = (
            "explicit CLI"
            if transport in {"cli", "codex_cli"}
            else "streaming API" if configured_responses_streaming() else "non-streaming API"
        )
        print(
            f"codex_responses transport={response_mode}; the terminal may stay quiet "
            "until each complete LLM result is validated.",
            flush=True,
        )

    model = None
    if not args.disable_llm:
        model = LLMFactory.create_or_none(
            model_name=args.model_name,
            api_key=args.api_key,
            base_url=args.base_url,
        )
        if model is None:
            raise SystemExit("模型没有创建成功，请检查 model/base_url/api_key/wire-api 配置。")

    agent = ResearchAgent(
        model=model,
        contract_version=args.contract_version,
        use_llm=not args.disable_llm,
        knowledge_base_dir=args.knowledge_base_dir,
        memory_dir=args.memory_dir,
        max_survey_rounds=args.max_survey_rounds,
        knowledge_top_k=args.knowledge_top_k,
        memory_top_k=args.memory_top_k,
        enable_memory=args.enable_memory,
        enable_online_literature=(
            False
            if args.no_online_literature
            else (True if args.online_literature else None)
        ),
        literature_download_pdfs=args.download_pdfs,
        enable_web_search=(
            False if args.no_web_search else (True if args.web_search else None)
        ),
        **route_trust,
    )

    state = agent.run(
        event_type=args.event_type,
        query=query,
        constraints=constraints,
        payload=payload,
        previous_state=previous_state,
        campaign_id=campaign_id,
        reference_inputs=reference_inputs,
    )

    print_summary(state)
    append_plan_ledger(args, state)
    debug_log_path = write_debug_log(state, DEFAULT_LOG_DIR)
    print(f"\ndebug_state_log: {debug_log_path}")

    if args.print_state_json or args.save_state:
        state_json = build_state_json(state)
        if args.print_state_json:
            print("\nfull_state_json:")
            print(state_json)
        if args.save_state:
            output_path = Path(args.save_state).expanduser().resolve()
            output_path.write_text(state_json, encoding="utf-8")
            print(f"\nstate JSON saved to: {output_path}")

    return 0 if state.status in {"completed", "not_implemented"} else 1


if __name__ == "__main__":
    sys.exit(main())
