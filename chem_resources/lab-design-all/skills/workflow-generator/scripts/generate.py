import sys
import json
import re
import requests
import os


UNIT_SUFFIX_RE = re.compile(r"[（(]([^（）()]*)[)）]\s*$")
RANGE_RE = re.compile(r"\[\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*\]")
STEP_META_KEYS = {"step_number", "workstation", "operation", "parameters", "id", "source_macro_step",
                  "macro_action_id", "observation_point_id", "notes"}
PLATFORM_SCHEMA_FILENAME = "0410数据转换.txt"
NO_RANGE_PARAMS = {"容器编号", "开盖编号", "关盖编号", "容器数量"}


def _normalize_param_name(name):
    text = str(name or "").strip().lstrip("-").strip()
    return UNIT_SUFFIX_RE.sub("", text).strip()


def _workstation_skill_root():
    """Sibling chemistry-experiment-workstation skill inside the same bundle."""
    override = os.environ.get("WORKFLOW_TRUTH_SOURCE_DIR", "").strip()
    if override:
        return override if os.path.isdir(override) else ""
    candidate = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "..", "chemistry-experiment-workstation")
    )
    return candidate if os.path.isdir(candidate) else ""


def load_truth_source_schemas():
    """Parse SKILL.md parameter tables into {station: schema} keyed by EN and 中文 names.

    Schema: {"params": {name: [(low, high), ...]}, "confident": bool}
    Returns {} when the truth source directory is unavailable (validation then
    degrades to structural checks with a warning).
    """
    root = _workstation_skill_root()
    if not root:
        return {}

    name_map = {}
    mapping_path = os.path.join(root, "工作站名称中英文对照.md")
    if os.path.exists(mapping_path):
        with open(mapping_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if "\t" not in line:
                    continue
                english, chinese = [part.strip() for part in line.split("\t", 1)]
                if english and chinese and english != "英文名" and set(english) != {"-"}:
                    name_map[english] = chinese

    schemas = {}
    for module_dir in ("references-Synthesis-Module",
                       "references-Reaction-and-Testing-Module",
                       "references-Characterization-Module"):
        module_path = os.path.join(root, module_dir)
        if not os.path.isdir(module_path):
            continue
        for station_dir in os.listdir(module_path):
            skill_path = os.path.join(module_path, station_dir, "SKILL.md")
            if not os.path.exists(skill_path):
                continue
            with open(skill_path, "r", encoding="utf-8") as handle:
                content = handle.read()
            params = {}
            in_table = False
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped.startswith("|"):
                    in_table = False
                    continue
                cells = [cell.strip() for cell in stripped.strip("|").split("|")]
                if cells and cells[0] in {"参数名", "参数"}:
                    in_table = True
                    continue
                if not in_table or set("".join(cells)) <= {"-", " ", ":"} or len(cells) < 5:
                    continue
                name = _normalize_param_name(cells[0].strip("*"))
                if not name:
                    continue
                ranges = params.setdefault(name, [])
                if name not in NO_RANGE_PARAMS:
                    match = RANGE_RE.search(" ".join(cells[1:]))
                    if match:
                        ranges.append((float(match.group(1)), float(match.group(2))))
            if not params:
                continue
            schema = {"params": params, "confident": True}
            schemas[station_dir] = schema
            display = name_map.get(station_dir, "")
            if display:
                schemas[display] = schema
                # "常温磁力搅拌工作站_V1" should also match "常温磁力搅拌工作站"
                schemas[display.split("_V")[0]] = schema

    # The platform's own schema export uses PLATFORM station names
    # (303物料站, 移液平台1ml_V2, …) and exact per-version parameter keys —
    # the form dispatch payloads arrive in. Register it as first-class truth.
    platform_path = os.path.join(root, PLATFORM_SCHEMA_FILENAME)
    if os.path.exists(platform_path):
        try:
            with open(platform_path, "r", encoding="utf-8") as handle:
                platform_data = json.load(handle)
        except (ValueError, OSError):
            platform_data = {}
        for step in platform_data.get("steps", []) or []:
            station = str(step.get("workstation", "")).strip()
            if not station:
                continue
            schema = schemas.setdefault(station, {"params": {}, "confident": True})
            for param in step.get("parameters", []) or []:
                name = _normalize_param_name(param.get("parameter_name", ""))
                if name:
                    schema["params"].setdefault(name, [])
    return schemas


def _normalize_station_name(name):
    return str(name or "").replace("_", "").replace(" ", "").lower()


def _resolve_station_schema(schemas, station_name):
    name = str(station_name or "").strip()
    if not name:
        return None
    if name in schemas:
        return schemas[name]
    # underscore/case-insensitive exact match (移液平台1ml_V2 vs 移液平台_1ml_V2)
    normalized = _normalize_station_name(name)
    for alias, schema in schemas.items():
        if _normalize_station_name(alias) == normalized:
            return schema
    for alias, schema in schemas.items():
        norm_alias = _normalize_station_name(alias)
        if norm_alias and (norm_alias in normalized or normalized in norm_alias):
            return schema
    return None


def validate_steps_against_truth_source(steps, schemas):
    """Strict dispatch-field validation: unknown workstation / unknown field /
    out-of-range value are errors. Returns a list of error strings."""
    errors = []
    for step in steps:
        step_no = step.get("step_number", "?")
        station = str(step.get("workstation", "")).strip()
        for key in step:
            if key not in STEP_META_KEYS:
                errors.append("Step %s: unknown step field `%s`" % (step_no, key))
        schema = _resolve_station_schema(schemas, station)
        if schema is None:
            errors.append(
                "Step %s: workstation `%s` is not in the truth source" % (step_no, station)
            )
            continue
        parameters = step.get("parameters")
        if parameters is None:
            continue
        if not isinstance(parameters, dict):
            errors.append("Step %s: parameters must be an object" % step_no)
            continue
        known = schema["params"]
        for raw_name, value in parameters.items():
            name = _normalize_param_name(raw_name)
            base = re.sub(r"[一二三四五六七八九十0-9]+$", "", name) or name
            if name not in known and base not in known:
                errors.append(
                    "Step %s (%s): parameter `%s` is not a dispatchable field"
                    % (step_no, station, raw_name)
                )
                continue
            ranges = known.get(name) or known.get(base) or []
            number = None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                number = float(value)
            elif isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value.strip()):
                number = float(value.strip())
            if ranges and number is not None:
                if not any(low <= number <= high for low, high in ranges):
                    bounds = " / ".join("[%g,%g]" % pair for pair in ranges)
                    errors.append(
                        "Step %s (%s): `%s`=%g outside allowed range %s"
                        % (step_no, station, raw_name, number, bounds)
                    )
    return errors


def generate_workflow(url, request_body: dict):
    """
    Generate workflow by calling the workstation data parsing service.

    Args:
        url: The API endpoint URL
        request_body: Request body containing experiment steps and plan name

    Returns:
        dict: Response data from the service

    Raises:
        Exception: If the API returns an error
    """
    headers = {
        "Content-Type": "application/json"
    }

    # Send POST request with JSON data
    response = requests.post(url, json=request_body, headers=headers)
    response.raise_for_status()

    result = response.json()

    # Check if the response contains an error
    if result.get("code") != 200:
        raise Exception(result.get("message", "Unknown error occurred"))

    return result


def validate_experiment_steps(steps):
    """
    Validate the structure of experiment steps.

    Args:
        steps: List of experiment step objects

    Returns:
        bool: True if valid, raises exception otherwise
    """
    if not isinstance(steps, list):
        raise ValueError("experiment_steps.steps must be an array")

    for step in steps:
        if "step_number" not in step:
            raise ValueError("Each step must have a step_number")
        if "workstation" not in step:
            raise ValueError("Each step must have a workstation")
        if "operation" not in step:
            raise ValueError("Each step must have an operation")

        # Validate step_number is a positive integer
        if not isinstance(step["step_number"], int) or step["step_number"] <= 0:
            raise ValueError(f"step_number must be a positive integer, got: {step['step_number']}")

    # Strict truth-source validation: unknown workstations, non-dispatchable
    # parameter fields, and out-of-range values must not reach the service.
    schemas = load_truth_source_schemas()
    if not schemas:
        print("Warning: workstation truth source unavailable; skipping strict parameter validation")
    else:
        schema_errors = validate_steps_against_truth_source(steps, schemas)
        if schema_errors:
            raise ValueError(
                "dispatch parameter validation failed:\n" + "\n".join(schema_errors)
            )

    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python generate.py '<JSON>'")
        print("\nExample:")
        print('python generate.py \'{"experiment_steps": {"steps": [{"step_number": 1, "workstation": "物料站", "id":1427568512205824, "operation": "物料拿取", "parameters": {"容器类型": "进样瓶", "容器编号": [1, 2]}}], "unknown_steps": null}, "plan_name": "test_plan"}\'')
        sys.exit(1)

    query = sys.argv[1]
    parse_data = {}

    try:
        parse_data = json.loads(query)
        print(f"Successfully parsed request body")
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        sys.exit(1)

    # Validate required fields
    if "experiment_steps" not in parse_data:
        print("Error: experiment_steps must be present in request body.")
        sys.exit(1)

    if "steps" not in parse_data["experiment_steps"]:
        print("Error: experiment_steps.steps must be present in request body.")
        sys.exit(1)

    if "plan_name" not in parse_data:
        print("Error: plan_name must be present in request body.")
        sys.exit(1)

    # Validate experiment steps structure
    try:
        validate_experiment_steps(parse_data["experiment_steps"]["steps"])
    except ValueError as e:
        print(f"Validation error: {e}")
        sys.exit(1)

    # Get token from environment variable or request body
    token = os.environ.get("AICHEM_APP_TOKEN", "")

    # Build request body
    request_body = {
        "experiment_steps": parse_data["experiment_steps"],
        "plan_name": parse_data["plan_name"]
    }
    if token:
        request_body["token"] = token

    # API endpoint host:port from environment variable (without scheme), defaulting to original value if not set
    host_port = os.environ.get("WORKFLOW_SERVICE_URL", "192.168.90.243:8009")
    api_url = f"http://{host_port.rstrip('/')}/parse_workstation"

    try:
        result = generate_workflow(api_url, request_body)
        print(json.dumps(result, indent=2, ensure_ascii=False))

        # Print template_id for easy access
        if result.get("code") == 200 and result.get("data") and "template_id" in result["data"]:
            print(f"\n✓ Workflow generated successfully!")
            print(f"Template ID: {result['data']['template_id']}")
            print(f"Please use this ID to view the workflow on the platform.")
    except requests.exceptions.RequestException as e:
        print(f"Network error: {str(e)}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {str(e)}")
        sys.exit(1)
