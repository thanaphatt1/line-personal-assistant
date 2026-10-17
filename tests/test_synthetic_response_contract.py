"""Response parsing preserves literal arguments; business tools validate them later."""
import pytest
from line_assistant.history_first import parse_response
from test_history_first import response

@pytest.mark.parametrize('arguments', [
 '{"draft_ref":null,"entries":[]}',
 '{ "draft_ref" : null, "entries" : [{"item":"สินค้าทดสอบ","amount":47}] }',
 '{"draft_ref":null,"entries":[{"item":"สินค้าทดสอบ","amount":"47.00"}]}',
 '{"draft_ref":null,"entries":[{"item":null,"amount":null,"note":""}]}',
 r'{"draft_ref":null,"entries":[{"note":"line1\nline2","item":"\u0e01"}]}',
])
def test_wire_parser_keeps_original_argument_string_without_granting_authority(arguments):
 value=response(name='prepare_expenses',args={});value['output'][0]['arguments']=arguments
 parsed=parse_response(value,allowed_tools={'prepare_expenses'},max_input_tokens=2000)
 assert parsed['call']['arguments']==arguments
