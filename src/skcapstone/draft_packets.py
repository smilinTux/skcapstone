"""Version-bound draft persistence and safe PDF/DOCX packet export.

The module deliberately uses only the standard library.  Draft and artifact
metadata are append-only JSON-lines events; every line is serialized and parsed
before it is committed.  Templates are named, application-owned values, never
filesystem paths supplied by a model.
"""
from __future__ import annotations

import hashlib, hmac, io, json, os, secrets, zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

TEMPLATES = {"plain": "{body}", "legal": "{title}\n\n{body}"}

def _now(): return datetime.now(timezone.utc).isoformat()
def _hash(data): return hashlib.sha256(data).hexdigest()
def _json_line(obj):
    line = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    json.loads(line)
    return line + "\n"

class DraftError(ValueError): pass
class AuthorizationError(PermissionError): pass

@dataclass(frozen=True)
class Artifact:
    matter_id: str
    version: int
    format: str
    sha256: str
    path: str
    manifest_sha256: str

class DraftStore:
    def __init__(self, root):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.events = self.root / "events.jsonl"

    def _append(self, event):
        line = _json_line({"ts": _now(), **event})
        with self.events.open("a", encoding="utf-8") as f:
            f.write(line); f.flush(); os.fsync(f.fileno())

    def _events(self):
        if not self.events.exists(): return []
        out=[]
        for line in self.events.read_text(encoding="utf-8").splitlines():
            if line.strip(): out.append(json.loads(line))
        return out

    def save(self, matter_id, content, *, source_map=None, approval=None):
        if not matter_id or not isinstance(content, str): raise DraftError("invalid draft")
        previous = [e for e in self._events() if e.get("matter_id")==matter_id and e["type"]=="draft_saved"]
        version = len(previous)+1
        self._append({"type":"draft_saved", "matter_id":matter_id, "version":version,
                      "content":content, "source_map":source_map or {}, "approval":approval})
        return version

    def load(self, matter_id, version=None):
        drafts=[e for e in self._events() if e.get("matter_id")==matter_id and e["type"]=="draft_saved"]
        if not drafts: raise DraftError("draft not found")
        item = next((e for e in drafts if e["version"]==version), None) if version else drafts[-1]
        if item is None: raise DraftError("version not found")
        return dict(item)

    def approve(self, matter_id, version, approver):
        self.load(matter_id, version)
        self._append({"type":"approval", "matter_id":matter_id, "version":version, "approver":approver})

    def export(self, matter_id, version, fmt, *, token, template="plain", retention=30):
        if fmt not in {"pdf","docx"} or template not in TEMPLATES: raise DraftError("unsupported output")
        if not hmac.compare_digest(token, os.environ.get("SK_PACKET_TOKEN", "")): raise AuthorizationError("invalid token")
        draft=self.load(matter_id, version)
        approvals=[e for e in self._events() if e.get("type")=="approval" and e.get("matter_id")==matter_id and e.get("version")==version]
        body=TEMPLATES[template].format(title=matter_id, body=draft["content"])
        data = _pdf(body) if fmt=="pdf" else _docx(body)
        outdir=self.root / "artifacts" / matter_id / str(version); outdir.mkdir(parents=True, exist_ok=True)
        path=outdir / ("packet."+fmt); path.write_bytes(data)
        manifest={"matter_id":matter_id,"version":version,"format":fmt,"template":template,"source_sha256":_hash(draft["content"].encode()),"artifact_sha256":_hash(data),"approved":bool(approvals),"retention_days":retention,"created_at":_now()}
        mp=outdir/"manifest.json"; mp.write_text(json.dumps(manifest,sort_keys=True,indent=2)+"\n",encoding="utf-8")
        manifest["manifest_sha256"]=_hash(mp.read_bytes())
        self._append({"type":"artifact_created", **manifest, "path":str(path)})
        return Artifact(matter_id,version,fmt,manifest["artifact_sha256"],str(path),manifest["manifest_sha256"])

def _pdf(text):
    # Bounded one-page-per-1000 characters renderer, with deterministic objects.
    pages=[text[i:i+1000] for i in range(0,max(1,len(text)),1000)]
    streams=[]
    for page in pages:
        safe=page.replace("\\","\\\\").replace("(","\\(").replace(")","\\)").replace("\n"," ")
        streams.append(f"BT /F1 10 Tf 50 750 Td ({safe}) Tj ET".encode())
    obj=[b"<< /Type /Catalog /Pages 2 0 R >>", None, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids=[]
    for s in streams:
        si=len(obj)+1; obj.append(f"<< /Length {len(s)} >>\nstream\n".encode()+s+b"\nendstream"); pi=len(obj)+1; obj.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {si+1} 0 R >> >> /Contents {si} 0 R >>".encode()); kids.append(f"{pi} 0 R")
    obj[1]=f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    out=b"%PDF-1.4\n"; offsets=[]
    for n,o in enumerate(obj,1): offsets.append(len(out)); out+=f"{n} 0 obj\n".encode()+o+b"\nendobj\n"
    x=len(out); out+=f"xref\n0 {len(obj)+1}\n0000000000 65535 f \n".encode()+b''.join(f"{v:010d} 00000 n \n".encode() for v in offsets)+f"trailer << /Size {len(obj)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode(); return out

def _docx(text):
    xml=f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p></w:body></w:document>'
    b=io.BytesIO()
    with zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("word/document.xml",xml)
    return b.getvalue()
