"""Acceptance regressions; synthetic source and provider cases."""
import json
import socket

import pytest
from line_assistant.catalog import ItemCatalog

import language_support as evaluation
FIXTURE = evaluation.FIXTURE

@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Language diagnostics must not use the network')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)


@pytest.mark.parametrize('case', FIXTURE['cases'], ids=lambda case: case['id'])
def test_current_extraction_validation_and_router_baseline(case):
    h = evaluation.LanguageHarness(case['proposal'])
    result = h.send(case['text'],owner=case.get('owner','mock-owner'))
    assert (result.status, len(h.sheets.entries), h.port.calls) == (case['expected_status'], case['expected_mock_rows'], case['expected_model_calls'])


def typo_harness():
    case = next(c for c in FIXTURE['cases'] if c['id'] == 'spelling_error')
    h = evaluation.LanguageHarness(case['proposal'])
    result = h.send(case['text'],owner=case.get('owner','mock-owner'))
    assert result.status == 'clarification' and not h.sheets.entries
    return h, result.data['token']


def test_plain_language_item_followup_uses_exact_master_without_model():
    h, token = typo_harness()
    result = h.send('หมายถึงรถเมล์ครับ')
    assert result.status == 'recorded'
    assert h.port.calls == 1 and len(h.sheets.entries) == 1


def test_explicit_selection_works_while_master_is_loaded():
    h, token = typo_harness()
    assert h.send(f'เลือก {token} รถเมล์').status == 'recorded'
    assert h.port.calls == 1 and len(h.sheets.entries) == 1


def test_empty_process_catalog_refreshes_before_answering_persisted_item_question():
    h, token = typo_harness()
    h.service.catalog = ItemCatalog([], {})  # The exact production bootstrap state after a cold start.
    result = h.send(f'เลือก {token} รถเมล์')
    assert result.status == 'recorded' and h.sheets.catalog_reads >= 2
    assert h.port.calls == 1 and len(h.sheets.entries) == 1


@pytest.mark.parametrize('text,expected', [('สรุปวันนี้', 'summary'), ('สรุปวันนี้หน่อย', 'summary')])
def test_pending_summary_understands_supported_colloquial_read_without_model(text, expected):
    h, _ = typo_harness()
    assert h.send(text).status == expected and h.port.calls == 1
    assert not h.sheets.entries


def test_date_context_is_single_message_with_bounded_candidates_and_no_history():
    case = next(c for c in FIXTURE['cases'] if c['id'] == 'colloquial_expense')
    h = evaluation.LanguageHarness(case['proposal'])
    h.send(case['text'])
    sent = h.port.bodies[0]
    assert len(sent['input']) == 1 and '2026-10-12' in sent['input'][0]['content']
    assert 'previous_response_id' not in sent and '9x widget' not in json.dumps(sent, ensure_ascii=False)
    assert 'catalog_candidates' in sent['input'][0]['content'] and 'รถเมล์' in sent['input'][0]['content']


def test_same_webhook_event_does_not_repeat_model_or_mock_effect():
    case = next(c for c in FIXTURE['cases'] if c['id'] == 'colloquial_expense')
    h = evaluation.LanguageHarness(case['proposal'])
    event = h.event(case['text'])
    first = h.service.handle(event)
    assert first.status == 'recorded' and h.service.handle(event) == first
    assert h.port.calls == 1 and len(h.sheets.entries) == 1
