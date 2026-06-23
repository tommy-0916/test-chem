"""Stub for utils.adapter_data — README lists this file but it was missing in this checkout.

adapter_data(rows, field_configs) reshapes each row's keys per field_configs:
  - field_configs key  = source key from API response
  - field_configs val  = {"name": <new_key>, "converter": <optional fn>}

Rows not in field_configs are dropped from each item. Order follows field_configs declaration.
"""

from typing import Any, Callable, Dict, List, Optional


def adapter_data(rows: List[Dict[str, Any]],
                 field_configs: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        item: Dict[str, Any] = {}
        for src_key, cfg in field_configs.items():
            new_key: str = cfg.get("name", src_key)
            converter: Optional[Callable[[Any], Any]] = cfg.get("converter")
            val = row.get(src_key)
            if converter is not None:
                try:
                    val = converter(val)
                except Exception:
                    pass
            item[new_key] = val
        out.append(item)
    return out
