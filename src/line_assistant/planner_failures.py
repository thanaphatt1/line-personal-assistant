"""Fixed planner diagnostics and owner replies, independent of orchestration."""

from collections.abc import Mapping
from typing import NotRequired, TypedDict


class PlannerOutputLimit(ValueError):
    pass


class PlannerFailure(TypedDict):
    kind: str
    http_status: NotRequired[int]


def planner_failure(error: BaseException) -> PlannerFailure:
    """Fixed diagnostics only: never copy provider bodies or exception prose."""
    import httpx
    from .transport import ApiError, OutcomeUnknown

    kind = "unavailable"
    status: int | None = None
    if isinstance(error, PlannerOutputLimit):
        kind = "output_limit"
    elif isinstance(error, httpx.TimeoutException) or isinstance(error, TimeoutError):
        kind = "timeout"
    elif isinstance(error, OutcomeUnknown):
        kind = error.reason or "outcome_unknown"
    elif isinstance(error, ApiError):
        status = error.status
        kind = (
            "rate_limited"
            if status == 429
            else "provider_error"
            if status >= 500
            else "provider_rejected"
        )
    elif isinstance(error, httpx.HTTPError):
        kind = "transport"
    elif isinstance(error, ValueError):
        kind = "invalid_response"
    result: PlannerFailure = {"kind": kind}
    if status is not None:
        result["http_status"] = status
    return result


def planner_failure_message(detail: Mapping[str, object]) -> str:
    # The planner never writes; say so plainly instead of naming internals.
    retry = "ยังไม่ได้บันทึกหรือสร้างอะไรครับ ลองส่งข้อความเดิมอีกครั้งได้เลยครับ"
    kind = detail.get("kind")
    return {
        "timeout": "รอบนี้ใช้เวลาคิดนานเกินไปครับ " + retry,
        "transport": "ตอนนี้ผมตอบกลับไม่ได้ชั่วคราวครับ " + retry,
        "rate_limited": "ตอนนี้มีคำขอเข้ามามากเกินไปชั่วคราวครับ ยังไม่ได้บันทึกหรือสร้างอะไร ลองอีกครั้งในอีกสักครู่ได้เลยครับ",
        "provider_error": "ตอนนี้ผมตอบกลับไม่ได้ชั่วคราวครับ " + retry,
        "invalid_response": "รอบนี้ผมเรียบเรียงคำขอไม่สำเร็จครับ ยังไม่ได้บันทึกหรือสร้างอะไร ลองพิมพ์ใหม่หรือแยกเป็นทีละเรื่องได้ไหมครับ?",
        "response_too_large": "คำขอนี้ยาวเกินกว่าที่ผมทำได้ในรอบเดียวครับ ยังไม่ได้บันทึกหรือสร้างอะไร ลองแยกเป็นทีละเรื่องได้ไหมครับ?",
        "output_limit": "คำขอนี้ยาวเกินกว่าที่ผมทำได้ในรอบเดียวครับ ยังไม่ได้บันทึกหรือสร้างอะไร ลองแยกเป็นทีละเรื่องได้ไหมครับ?",
        "usage_bound": "รอบนี้ใช้การประมวลผลเกินขีดจำกัดที่ตั้งไว้ครับ ยังไม่ได้บันทึกหรือสร้างอะไร",
    }.get(kind if isinstance(kind, str) else "", "รอบนี้ผมทำต่อไม่สำเร็จครับ " + retry)
