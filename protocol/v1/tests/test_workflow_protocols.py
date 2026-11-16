import hashlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from protocol.v1.workflows.protocols import WORKFLOW_IDS, get_workflow_protocol
from protocol.v1.tests.test_mcp_http import HEADERS, _gateway, _jwt, _post, _verifier, signing_key
from protocol.v1.transport.mcp_http import create_http_app
from protocol.v1.transport.stdio_rpc import LocalToolRouter, RequestError


@pytest.mark.parametrize('workflow', WORKFLOW_IDS)
def test_delivery_is_versioned_methodology_not_execution(workflow):
    value = get_workflow_protocol(workflow)
    assert value['protocol_version'] == '2026-11-17.2'
    assert value['authority'] == 'methodology_only'
    for field in ('workflow_executed', 'state_changed', 'model_compliance_verified'):
        assert value[field] is False
    assert value['tool_hints_are_availability_claims'] is False
    assert value['source_coverage']['project_migration_complete'] is False
    assert len(value['source_coverage']['not_migrated']) == 3
    for document in value['documents']:
        assert hashlib.sha256(document['content'].encode()).hexdigest() == document['sha256']
        assert document['version'] in {'2026-11-05', '2026-11-16.1', '2026-11-17.2'}
    # The return is independent per call, so one client cannot corrupt later guidance.
    value['common_rules'].clear()
    assert get_workflow_protocol(workflow)['common_rules']


@pytest.mark.parametrize('workflow', ['../../secrets', '/etc/passwd', '', 'private_project', None])
def test_arbitrary_source_or_path_is_never_a_workflow(workflow):
    with pytest.raises(ValueError, match='Unknown workflow'):
        get_workflow_protocol(workflow)


def test_changed_bundled_source_fails_closed(tmp_path, monkeypatch):
    import protocol.v1.workflows.protocols as module
    package = Path(module.__file__).parent
    (tmp_path / 'sources').mkdir()
    (tmp_path / 'sources.json').write_bytes((package / 'sources.json').read_bytes())
    (tmp_path / 'sources/Operating_Contract.ko.md').write_bytes((package / 'sources/Operating_Contract.ko.md').read_bytes())
    (tmp_path / 'sources/04_Stock_Analysis_Protocol.md').write_text('tampered methodology')
    monkeypatch.setattr(module, 'files', lambda _: tmp_path)
    with pytest.raises(RuntimeError, match='integrity check failed'):
        get_workflow_protocol('stock_analysis')


def test_stdio_and_authenticated_http_deliver_the_same_contract(signing_key):
    gateway = _gateway()
    router = LocalToolRouter(gateway)
    local = router.call_tool('get_workflow_protocol', {'workflow': 'stock_analysis'})
    with TestClient(create_http_app(gateway, _verifier(signing_key)), base_url='https://mcp.example.test') as client:
        headers = {**HEADERS, 'Authorization': 'Bearer ' + _jwt(signing_key)}
        listing = _post(client, 'tools/list', headers=headers).json()['result']['tools']
        tool = next(t for t in listing if t['name'] == 'get_workflow_protocol')
        assert tool['annotations']['readOnlyHint'] is True
        result = _post(client, 'tools/call', {'name': 'get_workflow_protocol', 'arguments': {
            'workflow': 'stock_analysis'}}, headers=headers).json()['result']
        assert result['structuredContent'] == local
        assert not result.get('isError')
        assert _post(client, 'tools/list', headers=HEADERS).status_code == 401
        # An authenticated account without metadata permission cannot retrieve it.
        denied = create_http_app(gateway, _verifier(signing_key, permissions=['portfolio.read']))
        with TestClient(denied, base_url='https://mcp.example.test') as narrow:
            assert 'get_workflow_protocol' not in {t['name'] for t in _post(narrow, 'tools/list', headers=headers).json()['result']['tools']}
            assert _post(narrow, 'tools/call', {'name': 'get_workflow_protocol', 'arguments': {
                'workflow': 'stock_analysis'}}, headers=headers).json()['result']['isError']
        for args in [{'workflow': '../../secrets'}, {'workflow': 'overview', 'path': '/etc/passwd'}]:
            assert _post(client, 'tools/call', {'name': 'get_workflow_protocol', 'arguments': args}, headers=headers).json()['result']['isError']


def test_important_project_behaviors_survive_source_delivery():
    stock = next(d['content'] for d in get_workflow_protocol('stock_analysis')['documents'] if d['source_id'] == 'stock_analysis')
    reflection = next(d['content'] for d in get_workflow_protocol('reflection_pre')['documents'] if d['source_id'] == 'decision_reflection')
    assert '기업이 좋아지는가 / 지금 가격이 매력적인가 / 지금 진입할 조건인가 / 이 사용자에게 적합한가' in stock
    assert '미연결 계좌나 미확인 현금을 0으로 두지 않는다' in stock
    assert '이후 보도를 원인으로 확정하지 않는다' in stock
    assert '질문은 한 번에 하나' in reflection
    assert '세션 ID 또는 초안은 영구 회고 저장의 증거가 아니다' in reflection
    registration = get_workflow_protocol('knowledge_registration')
    assert any('보관 실패' in item for item in registration['steps'])
    assert any('approval_requirement' in item for item in registration['common_rules'])
    assert 'get_knowledge_generation' in registration['tool_hints']


def test_board_and_confirmation_adaptations_are_packaged_without_private_originals():
    result = get_workflow_protocol('strategy_board')
    docs = {item['source_id']: item['content'] for item in result['documents']}
    assert {'operating_contract', 'strategy_board', 'presentation', 'stock_analysis'} <= docs.keys()
    assert '가용 현금이 가장 큰 계좌' in docs['strategy_board']
    assert '미리보기 검토를 시작할 신호이며 적용 승인 자체가 아니다' in docs['operating_contract']
    assert '특정 제품의' in docs['presentation']
    assert result['source_coverage']['private_project_instructions_retrieved_completely'] is False
    serialized = str(result)
    assert '/volume1/' not in serialized and '192.168.' not in serialized
