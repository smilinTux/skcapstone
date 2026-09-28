import json
import pytest
from skcapstone.draft_packets import DraftStore, AuthorizationError

def test_reload_source_map_and_exports(tmp_path, monkeypatch):
    monkeypatch.setenv("SK_PACKET_TOKEN", "secret")
    s=DraftStore(tmp_path)
    assert s.save("M1", "hello", source_map={"x":"doc:1"}) == 1
    assert s.load("M1")["source_map"] == {"x":"doc:1"}
    pdf=s.export("M1",1,"pdf",token="secret")
    docx=s.export("M1",1,"docx",token="secret")
    assert pdf.sha256 and docx.sha256 and pdf.path.endswith(".pdf")
    assert json.loads((tmp_path/"events.jsonl").read_text().splitlines()[-1])["type"] == "artifact_created"

def test_auth_and_wrong_matter(tmp_path, monkeypatch):
    monkeypatch.setenv("SK_PACKET_TOKEN", "secret")
    s=DraftStore(tmp_path); s.save("M1", "x")
    with pytest.raises(AuthorizationError): s.export("M1",1,"pdf",token="bad")
    with pytest.raises(ValueError): s.export("M2",1,"pdf",token="secret")
