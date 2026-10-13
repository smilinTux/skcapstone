import hashlib
import pytest
from skcapstone.process_definitions import ProcessDefinition, ProcessRegistry, ProcessStep, ProcessBranch, SourceSpan, ProcessValidationError

H = hashlib.sha256(b'source').hexdigest()
def span(): return SourceSpan(source_id='doc', source_hash=H, start=0, end=6, wording='source')
def definition(**kw):
    step_kw = kw.pop('step', {})
    step = ProcessStep(id='start', wording=step_kw.pop('wording', 'start'), source=step_kw.pop('source', span()), **step_kw)
    return ProcessDefinition(process_id='demo', version='1.0.0', source_id='doc', source_hash=H, steps=(step,), **kw)

def test_same_hash_replay_and_changed_version():
    r = ProcessRegistry(); r.register_source('doc', b'source')
    d = definition(); assert r.register(d) is r.register(d)
    d2 = d.model_copy(update={'version':'2.0.0'}); r.register(d2)
    assert r.versions('demo') == ('1.0.0','2.0.0')

def test_source_mutation_and_contradiction_refused():
    r=ProcessRegistry(); r.register_source('doc', b'source'); r.register(definition())
    with pytest.raises(ProcessValidationError): r.register_source('doc', b'changed')
    with pytest.raises(ProcessValidationError): r.register(definition(step={'wording':'other'}))

def test_graph_and_references_validate():
    with pytest.raises(ValueError, match='cyclic'):
        ProcessDefinition(process_id='demo', version='1.0.0', source_id='doc', source_hash=H,
          steps=(ProcessStep(id='a',wording='a',source=span()),ProcessStep(id='b',wording='b',source=span())),
          branches=(ProcessBranch(id='x',from_step='a',to_step='b',condition='yes',source=span()),ProcessBranch(id='y',from_step='b',to_step='a',condition='yes',source=span())))
    with pytest.raises(ValueError, match='invalid capability'):
        definition(capabilities=(), step={'capability':'missing'})

def test_missing_source_span_and_unresolved_deadline():
    with pytest.raises(ValueError, match='bound'):
        definition(step={'source': SourceSpan(source_id='other', source_hash=H, start=0,end=1,wording='x')})
    with pytest.raises(ValueError, match='unresolved deadline'):
        definition(unresolved_values=('deadline',))
