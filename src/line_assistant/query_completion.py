"""One final-selection repair after a received planner protocol failure.

Never select an intermediate result automatically. The model can finish from
current evidence or clarify; it cannot fetch, prepare, confirm or write.
"""
from copy import deepcopy
import json

RECOVERABLE = {'missing_answer', 'text_bound', 'unknown_tool', 'invalid_call_id',
               'invalid_arguments_json', 'invalid_arguments_encoding', 'duplicate_json_keys', 'not_json',
               'object_arguments', 'one_serial_tool', 'incomplete_output', 'invalid_message'}


def feedback(host, last, diagnostic):
    summary = diagnostic.get('response_summary', {})
    if (not host.raw_history_enabled or not host._query_analysis_active
            or diagnostic.get('kind') != 'invalid_response'
            or diagnostic.get('validation_code') not in RECOVERABLE
            or summary.get('status') != 'completed' or summary.get('refusal_count', 0)):
        return None
    # Only a successful derived query qualifies, not exploration-only source
    # reads or an unrelated old result left over after a failed new query.
    if (not isinstance(last, dict) or not last.get('relational_query')
            or not last.get('ok') or not last.get('complete') or last.get('analysis_input_only')
            or last.get('read_only') is not True
            or last.get('financial_write_authorized') or last.get('calendar_write_authorized')
            or not last.get('scope', {}).get('sql')
            or last.get('query_ref') not in host._query_reads):
        return None
    if any(not row.get('complete') or row.get('calendar_selection', {}).get('uncertain_count')
           for row in host._query_reads.values()):
        return None
    return {'kind': 'planner_response', 'validation_code': diagnostic['validation_code'],
            'allowed_tools': ['act', 'finish_query_analysis'], 'source_reads_allowed': False,
            'required': ['The previous model response failed protocol validation and was not executed. '
                'Use only current executed results. Select results that actually answer ALL owner conditions '
                'and requested fields, and finish the answer now. Source-only or supporting results are not '
                'automatically final. If more analysis or an unresolved condition is needed, use clarify '
                'to explain that limitation without inventing data or asking for already supplied facts. '
                'No source reads, new calculations in prose, writes or confirmations.']}


def tools(definitions, query_refs):
    selected = []
    for tool in definitions:
        if tool['name'] == 'finish_query_analysis':
            value = deepcopy(tool)
            # Intent already belongs to the authenticated current turn. Do
            # not ask the model to repeat that proof or mix it with read refs.
            value['parameters']['properties'].pop('source_refs')
            value['parameters']['required'].remove('source_refs')
            value['parameters']['properties']['query_refs']['items']['enum'] = list(query_refs)
            selected.append(value)
        elif tool['name'] == 'act':
            variants = [v for v in tool['parameters']['properties']['request']['anyOf']
                        if v['properties']['intent']['enum'] == ['respond']]
            if variants:
                value = deepcopy(tool)
                value['parameters']['properties']['request']['anyOf'] = variants
                selected.append(value)
    return selected


def permits(call):
    if call['name'] == 'finish_query_analysis': return True
    if call['name'] != 'act': return False
    try: return json.loads(call['arguments'])['request']['intent'] == 'respond'
    except (KeyError, ValueError, TypeError): return False


def bind_owner(host, call):
    """Only the read-only finish operation gets this server-known intent proof."""
    if call['name'] != 'finish_query_analysis': return call
    value = deepcopy(call); args = json.loads(value['arguments'])
    args['source_refs'] = [host._current['source_ref']]
    value['arguments'] = json.dumps(args, ensure_ascii=False)
    return value
