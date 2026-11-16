"""Natural-note ingestion boundaries; synthetic source only, no live AI claims."""
from copy import deepcopy
import hashlib
import pytest
from protocol.v1.tests.test_knowledge_registration import gateway
from protocol.v1.runtime.knowledge_note import build_note_update, note_generation_id
from protocol.v1.runtime.native_knowledge_store import KnowledgeRejected, NativeKnowledgeStore

NOTE = {'title':'합성 운영 메모', 'source_text':'검증용 원문이다. 투자 사실이나 개인 정책이 아니다.\n원문 줄바꿈 보존.',
        'statements':['이 자료는 운영 검증용이다.']}


def test_natural_note_readback_restart_retry_and_conservative_metadata(tmp_path):
    g=gateway(tmp_path)
    result=g.register_knowledge(**NOTE)
    assert result['status']=='applied' and result['read_back']['matches']
    read=g.get_knowledge_generation(result['generation_id'],include_content=True)['content']
    assert read['current_state']=={}
    e=read['evidence'][0]
    assert e['content_ref_or_value']==NOTE['source_text']
    assert e['content_hash']==hashlib.sha256(NOTE['source_text'].encode()).hexdigest()
    assert e['verification']=='unverified' and e['freshness']=='unknown'
    assert e['lifecycle_scope']=='context_only'
    c=read['claims'][0]
    assert c['truth_status']=='unknown' and c['applicability_status']=='unresolved'
    assert 'valid_until' not in c and 'effective_at' not in c
    again=gateway(tmp_path).register_knowledge(**{**NOTE,'title':'새 제목','statements':['다른 요약']})
    assert again['status']=='already_registered'
    assert again['payload_digest']==result['payload_digest']
    assert again['summary_changes_applied'] is False
    assert len(NativeKnowledgeStore(tmp_path/'native').read_journal())==1


@pytest.mark.parametrize('changes', [
    {'source_text':''}, {'statements':[]}, {'statements':[' ']}, {'published_date':None},
    {'published_date':'2026-02-30'}, {'published_date':'20261117'}, {'source_url':'file:///private/secret'},
    {'source_url':'https://user:password@example.test/'}, {'current_state':{}},
    {'verified':True}, {'source_text':'한'*24000}, {'target_session_date':'2026-11-17'},
])
def test_bad_or_privileged_note_fields_cannot_write(tmp_path,changes):
    assert gateway(tmp_path).register_knowledge(**{**NOTE,**changes})['status']=='blocked'
    assert not NativeKnowledgeStore(tmp_path/'native').has_generations()


def test_note_never_bypasses_grant_or_combines_profiles(tmp_path):
    assert gateway(tmp_path,enabled=False).register_knowledge(**NOTE)['status']=='blocked'
    from protocol.v1.tests.test_knowledge_registration import update
    assert gateway(tmp_path).register_knowledge(update(),**NOTE)['status']=='blocked'


def test_explicit_publication_date_does_not_become_session_date():
    v=build_note_update({**NOTE,'published_date':'2026-10-20'})
    assert v['evidence'][0]['published_at']=={'value':'2026-10-20','precision':'date_only'}
    assert 'effective_at' not in v['claims'][0]


def test_stdio_advertises_only_content_fields_and_accepts_natural_note(tmp_path):
    from protocol.v1.transport.mcp_stdio import TradeMindMCPServer, _public_tool
    from protocol.v1.tests.test_mcp_write_surface import _call
    from protocol.v1.transport.stdio_rpc import LocalToolRouter
    g=gateway(tmp_path)
    tool=_public_tool(LocalToolRouter(g).tools['register_knowledge'])
    assert set(tool['inputSchema']['required'])=={'title','source_text','statements'}
    assert 'update' not in tool['inputSchema']['properties']
    result=_call(TradeMindMCPServer(g),'register_knowledge',NOTE,request_id='note')
    assert result['status']=='applied'
