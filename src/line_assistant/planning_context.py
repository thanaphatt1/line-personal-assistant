"""Assemble the planner request from current runtime capabilities and evidence.

This owns tool/context assembly and request-local views only. The caller owns
summary recovery, model accounting, loop decisions, confirmation and effects.
No model/provider request or consent decision is made here.
"""

from copy import deepcopy
from . import semantic_tools as semantic


def build_raw_request(
    host,
    at,
    event,
    exchanges,
    *,
    base_tools,
    enrollment,
    controls,
    repair_allowed=None,
    response_feedback=None,
):
    from . import raw_history as raw
    from .draft_workflow import definitions

    host.state.enable_raw_history()
    available = host._workflow()
    date_targets = semantic.date_targets(host)
    host._request_date_views = {v["date_ref"]: deepcopy(v) for v in date_targets}
    definitions_for_request = definitions(
        base_tools(
            enrollment=enrollment, semantic=host.semantic_enabled, controls=controls
        ),
        available,
        date_targets,
        previous_prices=host.previous_prices.enabled,
    )
    from .read_queries import context as query_context
    from .unified_query import context as unified_context
    from .intent_actions import (
        definitions as query_definitions,
        context as intent_context,
    )

    definitions_for_request = [
        t for t in definitions_for_request if t["name"] != "spending_summary"
    ] + query_definitions(host)
    from .query_analysis import definitions as analysis_definitions, READ_TOOLS

    definitions_for_request += analysis_definitions()
    from .relational import context as relation_context
    from .query_followups import (
        definitions as followup_definitions,
        context as followup_context,
    )

    definitions_for_request += followup_definitions(host)
    from .activity_labels import (
        definitions as label_definitions,
        context as label_context,
    )

    definitions_for_request += label_definitions(host)
    from .data_inventory import context as inventory_context

    if host.calendar_flow.enabled:
        definitions_for_request += host.calendar_flow.definitions()
    from .calendar_connections import (
        definitions as connection_definitions,
        context as connection_context,
    )

    if host.calendar_connections_enabled:
        definitions_for_request += connection_definitions()
    definitions_for_request += host.saved_edits.definitions()
    definitions_for_request += host.provisional_categories.definitions()
    definitions_for_request += host.historical_categories.definitions()
    definitions_for_request += host.table_flow.definitions()
    definitions_for_request += host.catalog_aliases.definitions()
    from .temporal import (
        definitions as temporal_definitions,
        context as temporal_context,
    )

    definitions_for_request += temporal_definitions()
    from .pending_records import (
        views as pending_views,
        definitions as pending_definitions,
        planner_tools,
    )

    pending = pending_views(host)
    host._pending_record_views = {v["draft_ref"]: deepcopy(v) for v in pending}
    definitions_for_request = planner_tools(definitions_for_request, pending)
    definitions_for_request += pending_definitions(pending)
    from .response_realization import repair_exit_definition

    definitions_for_request += repair_exit_definition(host)
    if repair_allowed is not None:
        definitions_for_request = [
            t for t in definitions_for_request if t["name"] in repair_allowed
        ]
    if host._query_analysis_active:
        definitions_for_request = [
            t for t in definitions_for_request if t["name"] in READ_TOOLS
        ]
    host.state.raw_tool_definitions = deepcopy(definitions_for_request)
    return raw.request(
        host.state,
        host._current["text"],
        at,
        event,
        definitions_for_request,
        exchanges,
        available_drafts=available,
        date_references=date_targets,
        calendar_drafts=host.calendar_flow.views()
        if host.calendar_flow.enabled
        else None,
        calendar_batches=host.calendar_flow.batches.views()
        if host.calendar_flow.enabled
        else None,
        production=getattr(host, "production", False),
        summary=host.state.rolling_summary,
        context_tokens=host.raw_context_tokens,
        query_context={
            **query_context(host),
            **unified_context(host),
            **intent_context(host),
            **followup_context(host),
            **label_context(host),
            **inventory_context(host),
            **connection_context(host),
            **host.saved_edits.context(),
            **host.provisional_categories.context(),
            **host.historical_categories.context(),
            **host.table_flow.context(),
            **host.catalog_aliases.context(),
            **temporal_context(host),
            **relation_context(host),
            **({"repair_feedback": response_feedback} if response_feedback else {}),
            "query_analysis_active": host._query_analysis_active,
            "pending_records": pending,
            "recent_recorded_expenses": semantic.recorded_expenses(host),
        },
    )
