"""Read/inspect/respond intents with server-owned execution and grounded replies.

The planner can inspect executed evidence and compose its answer in the same
call. This module never invokes another model or authorizes a business write.
"""
from copy import deepcopy
from dataclasses import asdict
import json

from pydantic import Field

from . import answer_composition, unified_query
from .history_context import digest
from .model_api import object_schema
from .query_analysis import IntentReview
from .tables import Strict
from .tool_errors import recovery

NAME = 'act'
MAX_RESPONSES = 8
MAX_RESPONSE_BYTES = 24_000


class Respond(Strict):
    response_ref: str = Field(min_length=1, max_length=64)
    review: IntentReview
    document: dict
    source_refs: list[str] = Field(min_length=1, max_length=16)


def _response(host, query_ref):
    from .query_analysis import finish, render
    from .response_realization import identity
    output = finish(host, {'query_refs': [query_ref], 'operation': 'report',
                          'source_refs': [host._current['source_ref']]})
    if not output.get('ok') or not output.get('complete'):
        return None
    core = render(output)
    facts = answer_composition.for_result(host, core)
    if facts is None:
        return None
    evidence=answer_composition.evidence(output)
    response_ref = 'answer_' + digest([host.settings.owner_id,host._active_event_id,query_ref,identity(core),facts,evidence])[:24]
    value={'response_ref': response_ref, 'query_ref': query_ref,
           'facts': facts, 'evidence': evidence}
    if len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())>MAX_RESPONSE_BYTES:
        return None
    return {**value,'core':core}


def response_views(host):
    views = {}
    if not getattr(host, 'response_realization_enabled', False):
        host._intent_responses = views
        return views
    # Build from current immutable query results only. No reads, reservations,
    # journals, consent changes or remote calls occur while offering replies.
    used=0
    for query_ref in reversed(list(getattr(host, '_query_reads', {}))[-MAX_RESPONSES:]):
        value = _response(host, query_ref)
        if value is not None:
            size=len(json.dumps({k:v for k,v in value.items() if k!='core'},ensure_ascii=False,separators=(',',':')).encode())
            if used+size>MAX_RESPONSE_BYTES:continue
            used+=size
            views[value['response_ref']] = value
    views=dict(reversed(list(views.items())))
    host._intent_responses = views
    return views


def definitions(host):
    read = deepcopy(unified_query.definitions(host)[0]['parameters']['properties'])
    read.pop('continue_analysis')
    variants = [object_schema({'intent': {'type': 'string', 'enum': ['read']}, **read})]
    from .data_inventory import definitions as inventory
    inspect = deepcopy(inventory()[0]['parameters']['properties'])
    variants.append(object_schema({'intent': {'type': 'string', 'enum': ['inspect']}, **inspect}))
    responses = response_views(host)
    if responses:
        from .table_workflow import schema
        review = schema(IntentReview)
        variants.append(object_schema({
            'intent': {'type': 'string', 'enum': ['respond']},
            'response_ref': {'type': 'string', 'enum': list(responses)},
            'review': review,
            'document': answer_composition.schema(),
            'source_refs': {'type': 'array', 'minItems': 1, 'maxItems': 16, 'items': {'type': 'string'}},
        }))
    return [{'type': 'function', 'name': NAME, 'strict': True,
        'description': 'Submit a read-only intent. read declares configured sources and SQL; the server handles fetching and computation. inspect discovers available source capabilities. respond checks actual selection/output against the owner request and composes a grounded answer from an offered read_responses view in this same planning call. Use facts/cells for business values. No writes, confirmations or new access.',
        'parameters': object_schema({'request': {'anyOf': variants}})}]


def context(host):
    return {'read_responses': [{k: deepcopy(v) for k, v in value.items() if k != 'core'}
            for value in getattr(host, '_intent_responses', {}).values()]}


def _failure(code, *, required=()):
    return {'relational_query': True, 'query_analysis': True, 'ok': False, 'complete': False,
            'status': 'clarification', 'read_only': True, 'financial_write_authorized': False,
            'calendar_write_authorized': False, 'error_code': code,
            'message': 'แผนตอบยังไม่ตรงกับผลที่ตรวจสอบแล้วครับ',
            'recovery': recovery(kind='assistant_arguments', tools=[NAME], required=required)}


def execute(host, args):
    from .action_trace import emit, ref, span
    if not isinstance(args, dict) or set(args) != {'request'} or not isinstance(args['request'], dict):
        return _failure('invalid_intent')
    request = deepcopy(args['request'])
    intent = request.pop('intent', None)
    if intent not in {'read', 'inspect', 'respond'}:
        return _failure('invalid_intent')
    emit('intent_selected', intent=intent)
    if intent == 'read':
        return unified_query.execute(host, request)
    if intent == 'inspect':
        from .data_inventory import execute as inspect
        return inspect(host, request)
    try:
        plan = Respond.model_validate(request)
        host._owner_sources(plan.source_refs)
        if host._current['source_ref'] not in plan.source_refs:
            return _failure('response_owner_source_required')
        offered = getattr(host, '_intent_responses', {}).get(plan.response_ref)
        if offered is None:
            return _failure('response_reference_unavailable')
        # Rebuild the core from live request-local evidence, never from caller
        # facts or a saved result envelope supplied by the model.
        actual = _response(host, offered['query_ref'])
        if actual is None or actual['response_ref'] != plan.response_ref:
            return _failure('response_reference_unavailable')
        if not plan.review.selection_matches or not plan.review.output_matches or plan.review.unresolved_conditions:
            return _failure('analysis_intent_mismatch', required=[
                'Correct the read plan to preserve all owner conditions and presentation, or use finish_query_analysis clarify for genuinely unresolved conditions.'])
        with span('intent_reply_render'):
            text = answer_composition.render(plan.document, actual['facts'])
        from .response_realization import identity, apply
        result = apply(actual['core'], text, {'status': 'llm', 'mode': 'planner_intent',
            'core_hash': identity(actual['core']), 'response_ref': plan.response_ref})
        emit('intent_response', status='verified', response_ref=ref(plan.response_ref), query_ref=ref(offered['query_ref']))
        return {'ok': True, 'read_only': True, 'prepared_reply_result': asdict(result)}
    except answer_composition.AnswerError as error:
        emit('reply_validation', validation_code=str(error), status='repair_requested')
        return _failure(str(error), required=[
            'Correct the answer document against the same read_responses facts and records. Preserve every required value, order and unit; do not repeat source reads.'])
    except (ValueError, KeyError, TypeError):
        return _failure('invalid_intent_response')
