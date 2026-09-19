from __future__ import annotations

from typing import Any

from .common import PROTOCOL_VERSION, digest, result_envelope, timepoint


_PRE_STEPS = [
    ("situation", "지금 하려는 행동과 그 행동을 촉발한 상황을 분리해서 적는다.", ["considered_action"]),
    ("emotion", "지금 느끼는 감정과 행동 충동을 이름 붙인다.", ["emotions"]),
    ("assumption", "이 행동이 필요하다고 느끼게 하는 핵심 가정이나 자동 사고를 적는다.", ["assumptions"]),
    ("counterevidence", "그 가정과 반대되는 근거 또는 아직 확인되지 않은 사실을 찾는다.", ["counterevidence", "bias_candidates"]),
    ("wait", "지금 실행하지 않아도 되는 조건과 기다리면 확인 가능한 신호를 정한다.", ["waiting_conditions"]),
    ("conclusion", "분석을 마친 뒤 사용자가 선택한 결론을 기록한다.", ["user_conclusion"]),
]

_POST_STEPS = [
    ("action", "실제로 무엇을 했는지와 당시 의도했던 행동을 구분해 적는다.", ["considered_action"]),
    ("emotion", "결과를 안 지금의 감정과 당시 감정을 구분한다.", ["emotions"]),
    ("assumption", "당시 판단을 이끈 가정 중 무엇이 확인되거나 깨졌는지 적는다.", ["assumptions"]),
    ("process", "결과의 좋고 나쁨과 별개로 판단 과정에서 놓친 반대 근거와 편향 후보를 찾는다.", ["counterevidence", "bias_candidates"]),
    ("next", "다음에 같은 상황이 오면 기다릴 조건과 반복하거나 바꿀 규칙을 정한다.", ["waiting_conditions", "user_conclusion"]),
]


def start_reflection(
    mode: str,
    *,
    context: dict[str, Any] | None = None,
    started_at: dict[str, str] | None = None,
) -> dict[str, Any]:
    if mode not in {"pre", "post"}:
        raise ValueError("mode must be 'pre' or 'post'")
    started_at = started_at or timepoint()
    context_ref = context.get("context_id") if isinstance(context, dict) else None
    seed = {"mode": mode, "started_at": started_at, "context_ref": context_ref}
    steps = _PRE_STEPS if mode == "pre" else _POST_STEPS
    session = {
        "protocol_version": PROTOCOL_VERSION,
        "session_id": f"reflection-session:{digest(seed)[:24]}",
        "mode": mode,
        "method": {
            "method_id": "official.reflection.cbt.pre-post",
            "version": "1.0-draft",
            "framework": "CBT-informed investment decision reflection",
            "context_dependencies": [
                {"capability": "decision.history", "optional": True},
                {"capability": "portfolio.summary", "optional": True},
            ],
        },
        "started_at": started_at,
        "context_ref": context_ref,
        "steps": [
            {"step_id": step_id, "prompt": prompt, "captures": captures}
            for step_id, prompt, captures in steps
        ],
        "record_draft": {
            "considered_action": None,
            "emotions": [],
            "assumptions": [],
            "bias_candidates": [],
            "counterevidence": [],
            "waiting_conditions": [],
            "user_conclusion": None,
        },
    }
    return result_envelope(
        capability="reflection.session",
        producer="official.reflection.cbt",
        status="ok",
        data=session,
        authority="reflection_session",
        generated_at=started_at,
        freshness="current",
        source_mode="local_store",
        provenance=[{
            "source_type": "reflection_method",
            "source_id": "official.reflection.cbt.pre-post@1.0-draft",
            "producer": "official.reflection.cbt",
            "producer_version": "0.1.0",
        }],
        permissions_used=["reflection.write"],
    )
