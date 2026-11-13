"""Document retrieval for SOPs / storage rules / data sheets.
Uses Chroma if installed; otherwise falls back to keyword search so the app always runs.
Interface (index_document, delete_document, search) is the only thing app.py touches,
so a pgvector implementation can be swapped in later."""
import os, re, json
from pypdf import PdfReader
from docx import Document as Docx

DATA = os.path.join(os.path.dirname(__file__), "rag_data")
os.makedirs(DATA, exist_ok=True)
COLLECTION = "warehouse_sops"

try:
    import chromadb
    _client = chromadb.PersistentClient(path=os.path.join(DATA, "chroma"))
    _col = _client.get_or_create_collection(COLLECTION)
except Exception:
    _col = None
FALLBACK = os.path.join(DATA, "chunks.json")

def backend():
    return "chroma" if _col is not None else "keyword"

def extract_text(path):
    ext = path.rsplit(".", 1)[-1].lower()
    if ext in ("txt", "md", "csv"):
        return open(path, encoding="utf-8", errors="ignore").read()
    if ext == "pdf":
        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    if ext == "docx":
        return "\n".join(p.text for p in Docx(path).paragraphs)
    return ""

def chunk(text, size=700, overlap=100):
    text = re.sub(r"\s+", " ", text).strip()
    return [text[i:i + size] for i in range(0, len(text), size - overlap)] if text else []

def _load():
    return json.load(open(FALLBACK)) if os.path.exists(FALLBACK) else []

def index_document(doc_id, filename, doc_type, text):
    chunks = chunk(text)
    if _col is not None:
        if chunks:
            _col.add(ids=[f"{doc_id}-{i}" for i in range(len(chunks))], documents=chunks,
                     metadatas=[{"doc_id": doc_id, "filename": filename, "doc_type": doc_type}] * len(chunks))
    else:
        data = _load() + [{"doc_id": doc_id, "filename": filename, "doc_type": doc_type, "text": c} for c in chunks]
        json.dump(data, open(FALLBACK, "w"))
    return len(chunks)

def delete_document(doc_id):
    if _col is not None:
        _col.delete(where={"doc_id": doc_id})
    else:
        json.dump([c for c in _load() if c["doc_id"] != doc_id], open(FALLBACK, "w"))

def search(query, k=4):
    if _col is not None:
        if _col.count() == 0:
            return []
        r = _col.query(query_texts=[query], n_results=min(k, _col.count()))
        return [{"filename": m["filename"], "doc_type": m["doc_type"], "text": d}
                for d, m in zip(r["documents"][0], r["metadatas"][0])]
    words = set(re.findall(r"\w+", query.lower()))
    scored = sorted(((len(words & set(re.findall(r"\w+", c["text"].lower()))), c) for c in _load()),
                    key=lambda x: -x[0])
    return [c for s, c in scored[:k] if s > 0]
