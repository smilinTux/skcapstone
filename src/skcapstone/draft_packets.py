"""Version-bound draft persistence and safe PDF/DOCX packet export.

The module deliberately uses only the standard library.  Draft and artifact
metadata are append-only JSON-lines events; every line is serialized and parsed
before it is committed.  Templates are named, application-owned values, never
filesystem paths supplied by a model.
"""
from __future__ import annotations

import hashlib, hmac, io, json, os, re, zipfile
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

# Placeholders are deliberately limited to simple field names.  The escaped
# braces matter: a literal ``{{name}}`` is a missing input, not template code.
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_.-]*)\s*\}\}")

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
        # Validate the existing log before extending it.  This keeps the
        # append-only journal fail-closed if it was truncated or tampered with.
        if self.events.exists():
            for raw in self.events.read_text(encoding="utf-8").splitlines():
                if raw.strip():
                    json.loads(raw)
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
        if not isinstance(matter_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", matter_id):
            raise DraftError("invalid matter")
        if not isinstance(content, str): raise DraftError("invalid draft")
        previous = [e for e in self._events() if e.get("matter_id")==matter_id and e["type"]=="draft_saved"]
        version = len(previous)+1
        # A new version can never inherit approval for an older version.
        # Record invalidation as its own lifecycle event, rather than mutating
        # the old draft or treating its links as approval evidence.
        old_approvals = {e.get("version") for e in self._events()
                         if e.get("matter_id") == matter_id and e.get("type") == "approval"}
        self._append({"type":"draft_saved", "matter_id":matter_id, "version":version,
                      "content":content, "source_map":source_map or {}, "approval":approval})
        if old_approvals:
            self._append({"type":"approval_invalidated", "matter_id":matter_id,
                          "superseded_by":version, "versions":sorted(old_approvals)})
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
        if not isinstance(matter_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", matter_id):
            raise DraftError("invalid matter")
        if fmt not in {"pdf","docx"} or template not in TEMPLATES: raise DraftError("unsupported output")
        if not isinstance(token, str) or not hmac.compare_digest(token, os.environ.get("SK_PACKET_TOKEN", "")):
            raise AuthorizationError("invalid token")
        if not isinstance(retention, int) or isinstance(retention, bool) or not 0 <= retention <= 3650:
            raise DraftError("invalid retention")
        draft=self.load(matter_id, version)
        events = self._events()
        invalidated = any(e.get("type") == "approval_invalidated" and
                          e.get("matter_id") == matter_id and version in e.get("versions", [])
                          for e in events)
        approvals=[e for e in events if e.get("type")=="approval" and e.get("matter_id")==matter_id and e.get("version")==version]
        approvals = [] if invalidated else approvals
        missing = sorted(set(_PLACEHOLDER.findall(draft["content"])))
        if missing:
            raise DraftError("missing placeholders: " + ", ".join(missing))
        body=TEMPLATES[template].format(title=matter_id, body=draft["content"])
        outdir=self.root / "artifacts" / matter_id / str(version); outdir.mkdir(parents=True, exist_ok=True)
        path=outdir / ("packet."+fmt)
        try:
            data = _pdf(body) if fmt=="pdf" else _docx(body)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data); os.replace(tmp, path)
        except Exception as exc:
            self._append({"type":"render_failed", "matter_id":matter_id,
                          "version":version, "format":fmt, "error":str(exc)})
            raise DraftError("render failed") from exc
        source_map_bytes = json.dumps(draft.get("source_map", {}), sort_keys=True,
                                      separators=(",", ":")).encode()
        manifest={"matter_id":matter_id,"version":version,"format":fmt,"template":template,
                  "source_sha256":_hash(draft["content"].encode()),
                  "source_map_sha256":_hash(source_map_bytes),
                  "artifact_sha256":_hash(data),"approved":bool(approvals),
                  "retention_days":retention,"created_at":_now(),
                  "lineage":{"draft_event":"draft_saved", "source_map":draft.get("source_map", {})}}
        # The manifest hash is over its canonical, hash-free representation.
        # Keep the hash in the file as well so an independent reader can verify
        # identity without consulting the event journal.
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        manifest["manifest_sha256"] = _hash(canonical)
        mp=outdir/"manifest.json"
        manifest_line = _json_line(manifest)
        tmp_manifest = mp.with_suffix(".json.tmp")
        tmp_manifest.write_text(manifest_line, encoding="utf-8")
        # Parse the exact bytes that will be published before replacing the
        # manifest.  This mirrors the journal's serializer/parse invariant.
        json.loads(tmp_manifest.read_text(encoding="utf-8"))
        os.replace(tmp_manifest, mp)
        self._append({"type":"artifact_created", **manifest, "path":str(path)})
        return Artifact(matter_id,version,fmt,manifest["artifact_sha256"],str(path),manifest["manifest_sha256"])

    def download(self, matter_id, version, fmt, *, token):
        """Read only a previously authorized, version-bound artifact."""
        if not isinstance(token, str) or not hmac.compare_digest(token, os.environ.get("SK_PACKET_TOKEN", "")):
            raise AuthorizationError("invalid token")
        rows = [e for e in self._events() if e.get("type") == "artifact_created"
                and e.get("matter_id") == matter_id and e.get("version") == version
                and e.get("format") == fmt]
        if not rows:
            raise DraftError("artifact not found")
        row = rows[-1]
        path = Path(row["path"])
        if not path.is_absolute():
            path = self.root / path
        manifest_path = path.with_name("manifest.json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_hash = manifest.pop("manifest_sha256")
            if _hash(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()) != manifest_hash:
                raise ValueError("manifest hash mismatch")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise DraftError("manifest unavailable or corrupt") from exc
        if (manifest.get("matter_id") != matter_id or manifest.get("version") != version
                or manifest.get("format") != fmt or manifest.get("artifact_sha256") != row.get("artifact_sha256")
                or not path.is_file() or _hash(path.read_bytes()) != row.get("artifact_sha256")):
            raise DraftError("artifact unavailable or corrupt")
        self._append({"type":"artifact_downloaded", "matter_id":matter_id,
                      "version":version, "format":fmt, "artifact_sha256":row["artifact_sha256"]})
        return path.read_bytes()

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
        si=len(obj)+1; obj.append(f"<< /Length {len(s)} >>\nstream\n".encode()+s+b"\nendstream"); pi=len(obj)+1; obj.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {si} 0 R >>".encode()); kids.append(f"{pi} 0 R")
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
