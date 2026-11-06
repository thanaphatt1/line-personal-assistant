"""Fixed planner diagnostics and owner replies, independent of orchestration."""

from collections.abc import Mapping
from typing import NotRequired, TypedDict


class PlannerOutputLimit(ValueError):
    pass


class PlannerResponseError(ValueError):
    """A received envelope failed a fixed parser rule; never exception prose."""
    def __init__(self, code: str, summary: dict[str, object]) -> None:
        super().__init__(code)
        self.code = code
        self.summary = summary


class PlannerFailure(TypedDict):
    kind: str
    http_status: NotRequired[int]
    validation_code: NotRequired[str]
    response_summary: NotRequired[dict[str, object]]


def response_summary(value: object) -> dict[str, object]:
    """Only enums and bounded counts; no names, text, arguments or reasoning."""
    if not isinstance(value, dict): return {"status": "unknown"}
    statuses = {"completed", "incomplete", "failed", "cancelled", "queued", "in_progress"}
    status = value.get("status")
    summary: dict[str, object] = {"status": status if isinstance(status, str) and status in statuses else "unknown"}
    details = value.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if reason is not None:
        summary["incomplete_reason"] = reason if isinstance(reason, str) and reason in {"max_output_tokens", "content_filter"} else "unknown"
    output = value.get("output")
    if not isinstance(output, list): return summary
    summary["output_count"] = min(len(output), 10000)
    counts = {"reasoning": 0, "message": 0, "function_call": 0, "other": 0}
    argument_bytes = text_bytes = refusals = 0
    for item in output[:32]:
        if not isinstance(item, dict): counts["other"] += 1; continue
        kind = item.get("type")
        counts[kind if isinstance(kind, str) and kind in counts else "other"] += 1
        arguments = item.get("arguments")
        if isinstance(arguments, str): argument_bytes += len(arguments.encode("utf-8", errors="replace"))
        content = item.get("content")
        if isinstance(content, list):
            for part in content[:32]:
                if not isinstance(part, dict): continue
                if part.get("type") == "refusal": refusals += 1
                text = part.get("text")
                if isinstance(text, str): text_bytes += len(text.encode("utf-8", errors="replace"))
    summary.update(output_types=counts, argument_bytes=min(argument_bytes, 1000000),
                   text_bytes=min(text_bytes, 1000000), refusal_count=refusals)
    return summary


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
    if isinstance(error, PlannerResponseError):
        result['validation_code'] = error.code
        result['response_summary'] = error.summary
    if status is not None:
        result["http_status"] = status
    return result


def planner_failure_message(detail: Mapping[str, object]) -> str:
    # Report the failure without adding unrelated action-status claims.
    retry = "ลองส่งข้อความเดิมอีกครั้งได้เลยครับ"
    kind = detail.get("kind")
    return {
        "timeout": "รอบนี้ใช้เวลาคิดนานเกินไปครับ " + retry,
        "transport": "ตอนนี้ผมตอบกลับไม่ได้ชั่วคราวครับ " + retry,
        "rate_limited": "ตอนนี้มีคำขอเข้ามามากเกินไปชั่วคราวครับ ลองอีกครั้งในอีกสักครู่ได้เลยครับ",
        "provider_error": "ตอนนี้ผมตอบกลับไม่ได้ชั่วคราวครับ " + retry,
        "invalid_response": "รอบนี้ผมเรียบเรียงคำขอไม่สำเร็จครับ ลองพิมพ์ใหม่หรือแยกเป็นทีละเรื่องได้ไหมครับ?",
        "response_too_large": "คำขอนี้ยาวเกินกว่าที่ผมทำได้ในรอบเดียวครับ ลองแยกเป็นทีละเรื่องได้ไหมครับ?",
        "output_limit": "คำขอนี้ยาวเกินกว่าที่ผมทำได้ในรอบเดียวครับ ลองแยกเป็นทีละเรื่องได้ไหมครับ?",
        "usage_bound": "รอบนี้ใช้การประมวลผลเกินขีดจำกัดที่ตั้งไว้ครับ",
    }.get(kind if isinstance(kind, str) else "", "รอบนี้ผมทำต่อไม่สำเร็จครับ " + retry)
