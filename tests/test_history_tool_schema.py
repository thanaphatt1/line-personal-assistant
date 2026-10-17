"""Provider strict-schema invariant for every advertised history tool variant."""
import pytest
from line_assistant.history_first import tools

def strict_objects(schema):
    kind=schema.get('type')
    if kind=='object' or isinstance(kind,list) and 'object' in kind:
        assert schema.get('additionalProperties') is False
        assert set(schema.get('required',[]))==set(schema.get('properties',{}))
        for field in schema.get('properties',{}).values():
            strict_objects(field)
    if 'items' in schema:
        strict_objects(schema['items'])
    for variant in schema.get('anyOf',[]):
        strict_objects(variant)

@pytest.mark.parametrize('enrollment',[False,True])
@pytest.mark.parametrize('semantic',[False,True])
@pytest.mark.parametrize('controls',[False,True])
def test_advertised_strict_tool_objects_require_all_declared_fields(enrollment,semantic,controls):
    for tool in tools(enrollment=enrollment,semantic=semantic,controls=controls):
        assert tool['strict'] is True
        strict_objects(tool['parameters'])
