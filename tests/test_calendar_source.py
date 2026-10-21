"""Offline source closure and checksum verification; never an image build."""
from pathlib import Path
import importlib.util,json,tarfile
import pytest

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def packager(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'scripts'))
    spec=importlib.util.spec_from_file_location('calendar_source_fixture',ROOT/'scripts/prepare_calendar_source.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def test_bundle_is_deterministic_includes_calendar_and_excludes_runtime_data(tmp_path,packager):
    a=tmp_path/'a';b=tmp_path/'b'
    first=packager.prepare(ROOT,a);second=packager.prepare(ROOT,b)
    assert first==second and first['upload_build_deploy_authorized'] is False
    packager.verify(a)
    with tarfile.open(a/'source.tar.gz') as archive:
        names=set(archive.getnames())
        for module in ['calendar_dates','calendar_provider','calendar_transport','calendar_workflow','saved_edits','expense_edits','calendar_reschedule','saved_edit_transport','historical_categories','previous_prices','query_followups','tables','table_storage','table_workflow','action_trace']:
            path='src/line_assistant/'+module+'.py'
            assert archive.extractfile(path).read()==(ROOT/path).read_bytes()
        runtime_names={n for n in names if n.startswith('src/line_assistant/') and n.endswith('.py')}
        expected={n for n in packager.INPUTS if n.startswith('src/line_assistant/') and n.endswith('.py')}
        assert runtime_names==expected and len(runtime_names)==98
        path='src/line_assistant/response_realization.py'
        assert archive.extractfile(path).read()==(ROOT/path).read_bytes()
        assert not any(n.startswith(('tests/','local-data/','.git/','docs/')) or n in {'.env','.env.example','RELEASE_V11.json'} for n in names)
        build=json.loads(archive.extractfile('deploy/cloudbuild.json').read())
        assert 'docker build --file deploy/history-production.Dockerfile ' in build['steps'][0]['args'][-1]
        assert build['substitutions']['_REVIEWED_TAG']==build['substitutions']['_SOURCE_CHECKSUMS_SHA256']=='REQUIRES_REVIEW'

def test_source_tamper_and_output_reuse_are_rejected(tmp_path,packager):
    path=tmp_path/'source-review';packager.prepare(ROOT,path)
    (path/'source/src/line_assistant/calendar_workflow.py').write_text('tampered')
    with pytest.raises(packager.base.PackagingError):packager.verify(path)
    with pytest.raises(FileExistsError):packager.prepare(ROOT,path)
