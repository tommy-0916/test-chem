from __future__ import annotations

from device_agent.run_from_research_state import build_parser


def test_exp_id_argument_is_accepted() -> None:
    args = build_parser().parse_args(
        [
            "--research-state",
            "state.json",
            "--exp-id",
            "A01-20260718-013500",
        ]
    )

    assert args.exp_id == "A01-20260718-013500"
