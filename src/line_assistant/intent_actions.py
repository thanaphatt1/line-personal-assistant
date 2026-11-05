"""Read/inspect/respond intents with server-owned execution and grounded replies.

The planner can inspect executed evidence and compose its answer in the same
call. This module never invokes another model or authorizes a business write.
"""
from copy import deepcopy
from dataclasses import asdict
import json

from pydantic import Field

from . import answer_composition, plain_read_answers, unified_query
from .history_context import digest
from .model_api import object_schema
from .query_analysis import IntentReview
from .tables import Strict
from .tool_errors import recovery

NAME = 'act'
MAX_RESPONSES = 8
MAX_RESPONSE_BYTES = 24_000
MAX_ANSWER_CONTEXT_BYTES = 6_000


class Respond(Strict):
    response_ref: str = Field(min_length=1, max_length=64)
    document: dict
    # Read old proposals conservatively, but do not ask new planners to
    # self-attest correctness or repeat evidence already bound by response_ref.
    review: IntentReview | None = None
    source_refs: list[str] | None = Field(default=None, min_length=1, max_length=16)


class PlainRespond(Strict):
    response_ref: str = Field(min_length=1, max_length=64)
    answer: str = Field(min_length=1, max_length=4500)


def _answer_context(host, query_ref):
    """Selected observations for follow-up interpretation, never reusable data."""
    output=host._query_reads[query_ref]
    if isinstance(output.get('rows'),list):
        records=output['rows'];columns=output.get('columns',[])
    elif isinstance(output.get('events'),list):
        records=[{k:row[k] for k in ('title','start','end','all_day') if k in row} for row in output['events']]
        columns=[]
    else:return None
    value={'format':'read-answer-context-v1','domain':output['domain'],
        'columns':deepcopy(columns),'records':deepcopy(records),
        'result_count':output.get('count'),'shown':len(records),'requires_fresh_read':True}
    try:size=len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())
    except (ValueError,TypeError):return None
    return value if size<=MAX_ANSWER_CONTEXT_BYTES else None


def _response(host, query_ref):
    from .query_analysis import finish, render
    from .response_realization import identity
    output = finish(host, {'query_refs': [query_ref], 'operation': 'report',
                            'source_refs': [host._current['source_ref']]})
    if not output.get('ok') or not output.get('complete'):
        return None
    core = render(output)
    plain = plain_read_answers.enabled(host)
    facts = plain_read_answers.for_result(host, core) if plain else answer_composition.for_result(host, core)
    if facts is None:
        return None
    if core.data.get('read_query'):
        previous_answer=_answer_context(host,query_ref)
        if previous_answer is not None:core.data['read_query']['answer_context']=previous_answer
    evidence=answer_composition.evidence(output)
    response_ref = 'answer_' + digest([host.settings.owner_id,host._active_event_id,query_ref,identity(core),facts,evidence])[:24]
    value={'response_ref': response_ref, 'query_ref': query_ref,
           ('read_result' if plain else 'facts'): facts, 'evidence': evidence}
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
        variants.append(object_schema({
            'intent': {'type': 'string', 'enum': ['respond']},
            'response_ref': {'type': 'string', 'enum': list(responses)},
            **({'answer': plain_read_answers.schema()['properties']['answer']} if plain_read_answers.enabled(host)
               else {'document': answer_composition.schema()}),
        }))
    return [{'type': 'function', 'name': NAME, 'strict': True,
        'description': 'Submit a read-only intent. read declares configured sources and SQL; the server handles fetching and computation. inspect discovers available source capabilities. respond selects an offered current read_responses result and composes the final answer in this same planning call. Use its read_result for a natural answer when offered, otherwise use facts/cells. No writes, confirmations or new access.',
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
        plain = plain_read_answers.enabled(host)
        plan = PlainRespond.model_validate(request) if plain else Respond.model_validate(request)
        if not plain and plan.source_refs is not None:
            try:
                host._owner_sources(plan.source_refs)
            except ValueError:
                return _failure('response_owner_source_required', required=[
                      'Omit source_refs when responding; response_ref already binds the current owner and executed result.'])
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
        if not plain and plan.review is not None and (
                not plan.review.selection_matches or not plan.review.output_matches or plan.review.unresolved_conditions):
            return _failure('analysis_intent_mismatch', required=[
                'Correct the read plan to preserve all owner conditions and presentation, or use finish_query_analysis clarify for genuinely unresolved conditions.'])
        metadata={'status':'llm','mode':'planner_intent'}
        with span('intent_reply_render'):
            if plain:
                text = plain_read_answers.text(plan.answer)
                metadata['format'] = 'plain_read'
            else:
                try:
                    text = answer_composition.render(plan.document, actual['facts'])
                except answer_composition.AnswerError as error:
                    # Owner evidence and the current result proof have passed.
                    # Discard all rejected prose and render the same facts;
                    # a wording failure must not erase a complete selected answer.
                    text=answer_composition.render(answer_composition.fallback_document(actual['facts']),actual['facts'])
                    metadata.update(status='fallback',reason='invalid_answer_document',validation_code=str(error))
                    emit('reply_validation',validation_code=str(error),status='fallback')
                    emit('reply_fallback',reason='invalid_answer_document',validation_code=str(error))
        from .response_realization import identity, apply
        result = apply(actual['core'], text, {**metadata,
            'core_hash': identity(actual['core']), 'response_ref': plan.response_ref})
        emit('intent_response', status='composed' if plain else 'verified' if metadata['status']=='llm' else 'fallback', response_ref=ref(plan.response_ref), query_ref=ref(offered['query_ref']))
        return {'ok': True, 'read_only': True, 'prepared_reply_result': asdict(result)}
    except answer_composition.AnswerError as error:
        emit('reply_validation', validation_code=str(error), status='repair_requested')
        return _failure(str(error), required=[
            'Correct the answer document against the same read_responses facts and records. Preserve every required value, order and unit; do not repeat source reads.'])
    except (ValueError, KeyError, TypeError):
        return _failure('invalid_intent_response')
