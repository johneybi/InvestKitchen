"""Validated journal reuse must observe all writer changes and reject corruption."""
import json
import os
from pathlib import Path
import pytest
from protocol.v1.tests.test_knowledge_registration import gateway, update
from protocol.v1.runtime.native_knowledge_store import NativeKnowledgeStore, KnowledgeStoreCorrupt


def test_repeated_reads_avoid_reparse_without_returning_mutable_cache(tmp_path,monkeypatch):
    g=gateway(tmp_path);assert g.register_knowledge(update())['status']=='applied'
    store=NativeKnowledgeStore(tmp_path/'native')
    original=Path.open;opens=[]
    def observed(path,*args,**kwargs):
        if path==store.journal_path:opens.append(1)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',observed)
    first=store.read_journal();first[0]['request']['claims'][0]['statement']='caller mutation'
    assert store.read_journal()[0]['request']['claims'][0]['statement']!='caller mutation'
    assert store.get_generation(update()['generation_id'])['commit_status']=='committed'
    assert len(opens)==1


def test_other_writer_append_invalidates_cached_reads(tmp_path):
    g=gateway(tmp_path);assert g.register_knowledge(update())['status']=='applied'
    store=NativeKnowledgeStore(tmp_path/'native');assert len(store.read_journal())==1
    note={'title':'new note','source_text':'A separate synthetic source.','statements':['Synthetic.']}
    assert gateway(tmp_path).register_knowledge(**note)['status']=='applied'
    assert len(store.read_journal())==2
    assert store.latest_generation_id()!=update()['generation_id']


@pytest.mark.parametrize('change',['same_size_restored_mtime','atomic_replace','truncate'])
def test_corruption_never_hidden_by_cache(tmp_path,change):
    assert gateway(tmp_path).register_knowledge(update())['status']=='applied'
    store=NativeKnowledgeStore(tmp_path/'native');store.read_journal()
    path=store.journal_path;info=path.stat();raw=path.read_bytes()
    altered=raw.replace(b'Asset Alpha',b'Asset Omega')
    assert altered!=raw and len(altered)==len(raw)
    if change=='same_size_restored_mtime':
        path.write_bytes(altered);os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
    elif change=='atomic_replace':
        other=path.with_suffix('.candidate');other.write_bytes(altered)
        os.utime(other,ns=(info.st_atime_ns,info.st_mtime_ns));other.replace(path)
    else:path.write_bytes(raw[:len(raw)//2])
    with pytest.raises(KnowledgeStoreCorrupt):store.read_journal()
    assert store._validated_cache is None


def test_oversize_journal_does_not_remain_cached(tmp_path,monkeypatch):
    import protocol.v1.runtime.native_knowledge_store as module
    assert gateway(tmp_path).register_knowledge(update())['status']=='applied'
    monkeypatch.setattr(module,'MAX_VALIDATED_JOURNAL_CACHE_BYTES',1)
    store=NativeKnowledgeStore(tmp_path/'native');assert store.read_journal()
    assert store._validated_cache is None


def test_single_generation_and_overlay_never_expose_internal_nested_values(tmp_path):
    g = gateway(tmp_path)
    result = g.register_knowledge(update())
    store = NativeKnowledgeStore(tmp_path/'native')
    first = store.get_generation(result['generation_id'])
    first['content']['claims'][0]['conditions'].append('caller mutation')
    first['committed_at']['value'] = 'caller mutation'
    overlay = store.overlay()
    overlay['claims'][0]['conditions'].append('overlay mutation')
    overlay['evidence'][0]['source']['document_id'] = 'overlay mutation'
    overlay['knowledge_generation']['committed_at']['value'] = 'overlay mutation'
    again = store.get_generation(result['generation_id'])
    assert again['content']['claims'][0]['conditions'] == update()['claims'][0]['conditions']
    assert again['content']['evidence'][0]['source']['document_id'] == update()['evidence'][0]['source']['document_id']
    assert again['committed_at'] == result['committed_at']


@pytest.mark.parametrize('reader', ['get_generation', 'latest_generation_id', 'has_generations', 'overlay'])
def test_internal_projection_reads_still_revalidate_changed_journal(tmp_path, reader):
    g = gateway(tmp_path)
    result = g.register_knowledge(update())
    store = NativeKnowledgeStore(tmp_path/'native')
    store.read_journal()
    raw = store.journal_path.read_bytes()
    store.journal_path.write_bytes(raw.replace(b'Asset Alpha', b'Asset Omega'))
    with pytest.raises(KnowledgeStoreCorrupt):
        if reader == 'get_generation':
            store.get_generation(result['generation_id'])
        else:
            getattr(store, reader)()
    assert store._validated_cache is None
