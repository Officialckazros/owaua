"""SQLite FTS5 reference knowledge base."""

import json
import logging
import math
import re
import typing
from typing import List, Optional

from owaua import config, db
from owaua.services.llm_client import LLMError, llm

_LOG = logging.getLogger(__name__)

_HAS_FTS5: Optional[bool] = None
_READY = False

_WORD = re.compile(r"[a-z0-9]{2,}")


def _detect_fts5(c: typing.Any) -> bool:
    try:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        c.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except Exception:
        return False


def ensure() -> None:
    """Create the KB tables (idempotent). Safe to call on every access."""
    global _READY, _HAS_FTS5
    if _READY:
        return
    c = db.conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS kb_docs (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_id TEXT NOT NULL DEFAULT 'legacy:disabled',
        topic   TEXT NOT NULL DEFAULT 'general',
        title   TEXT,
        source  TEXT,
        tags    TEXT,
        content TEXT NOT NULL,
        created REAL NOT NULL
    );
    """)
    columns = {r["name"] for r in c.execute("PRAGMA table_info(kb_docs)").fetchall()}
    if "scope_id" not in columns:
        c.execute("ALTER TABLE kb_docs ADD COLUMN scope_id TEXT NOT NULL DEFAULT 'legacy:disabled'")
    if "embedding" not in columns:
        c.execute("ALTER TABLE kb_docs ADD COLUMN embedding TEXT")
    if "embedding_model" not in columns:
        c.execute("ALTER TABLE kb_docs ADD COLUMN embedding_model TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS idx_kb_scope_topic ON kb_docs(scope_id,topic)")
    _HAS_FTS5 = _detect_fts5(c)
    if _HAS_FTS5:
        c.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
            topic, title, tags, content,
            content='kb_docs', content_rowid='id'
        );
        CREATE TRIGGER IF NOT EXISTS kb_ai AFTER INSERT ON kb_docs BEGIN
            INSERT INTO kb_fts(rowid, topic, title, tags, content)
            VALUES (new.id, new.topic, new.title, new.tags, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS kb_ad AFTER DELETE ON kb_docs BEGIN
            INSERT INTO kb_fts(kb_fts, rowid, topic, title, tags, content)
            VALUES ('delete', old.id, old.topic, old.title, old.tags, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS kb_au AFTER UPDATE ON kb_docs BEGIN
            INSERT INTO kb_fts(kb_fts, rowid, topic, title, tags, content)
            VALUES ('delete', old.id, old.topic, old.title, old.tags, old.content);
            INSERT INTO kb_fts(rowid, topic, title, tags, content)
            VALUES (new.id, new.topic, new.title, new.tags, new.content);
        END;
        """)
    c.commit()
    _READY = True


def _chunk(text: str, size: int = 700, overlap: int = 120) -> List[str]:
    """Split text into overlapping passages near `size` characters."""
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    buf = ""
    for p in paras:
        if len(p) > size * 1.5:
            if buf:
                chunks.append(buf.strip())
                buf = ""
            start = 0
            while start < len(p):
                chunks.append(p[start : start + size].strip())
                start += max(1, size - overlap)
            continue
        if buf and len(buf) + len(p) + 2 > size:
            chunks.append(buf.strip())
            tail = buf[-overlap:] if overlap else ""
            buf = (tail + "\n" + p).strip()
        else:
            buf = (buf + "\n\n" + p).strip() if buf else p
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]


def ingest(
    content: str,
    topic: str = "general",
    title: str | None = None,
    source: str | None = None,
    tags: str | None = None,
    *,
    scope_id: str,
) -> int:
    """Chunk `content` and store each passage. Returns number of passages stored."""
    ensure()
    n = 0
    c = db.conn()
    for i, passage in enumerate(_chunk(content)):
        ptitle = title if title else None
        if title and i:
            ptitle = f"{title} ({i + 1})"
        c.execute(
            "INSERT INTO kb_docs(scope_id,topic,title,source,tags,content,created) "
            "VALUES(?,?,?,?,?,?,?)",
            (scope_id, topic.strip() or "general", ptitle, source, tags, passage, db.now()),
        )
        n += 1
    c.commit()
    return n


def _keywords(text: str) -> List[str]:
    seen: set[str] = set()
    out: list[str] = []
    for w in _WORD.findall((text or "").lower()):
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _fts_query(query: str) -> str:
    """Build a safe FTS5 match expression from free text."""
    kws = _keywords(query)
    return " OR ".join(f'"{w}"' for w in kws)


def search(query: str, k: int = 6, *, scope_id: str) -> List[dict[typing.Any, typing.Any]]:
    """Return up to `k` most relevant passages for `query`, best first."""
    ensure()
    c = db.conn()
    if _HAS_FTS5:
        match = _fts_query(query)
        if not match:
            return []
        try:
            rows = c.execute(
                "SELECT d.topic AS topic, d.title AS title, d.source AS source, "
                "d.content AS content, bm25(kb_fts) AS rank "
                "FROM kb_fts JOIN kb_docs d ON d.id = kb_fts.rowid "
                "WHERE kb_fts MATCH ? AND d.scope_id=? ORDER BY rank LIMIT ?",
                (match, scope_id, k),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            _LOG.debug("FTS query failed; using bounded keyword fallback", exc_info=True)
    kws = _keywords(query)
    if not kws:
        return []
    scored: list[typing.Any] = []
    for r in c.execute(
        "SELECT topic,title,source,content FROM kb_docs WHERE scope_id=?", (scope_id,)
    ).fetchall():
        low = (r["content"] or "").lower()
        hits = sum(1 for w in kws if w in low)
        if hits:
            scored.append((hits, dict(r)))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [d for _, d in scored[:k]]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return -1.0
    return dot / (left_norm * right_norm)


async def semantic_search(
    query: str,
    k: int = 6,
    *,
    scope_id: str,
    user_id: str | None = None,
) -> List[dict[typing.Any, typing.Any]]:
    """Semantically search explicitly ingested server documents with local vectors."""
    ensure()
    if not config.OPENAI_SEMANTIC_KB or not config.OPENAI_API_KEY:
        return search(query, k=k, scope_id=scope_id)
    c = db.conn()
    model = config.OPENAI_EMBEDDING_MODEL
    missing = c.execute(
        "SELECT id,content FROM kb_docs WHERE scope_id=? "
        "AND (embedding IS NULL OR embedding_model IS NULL OR embedding_model<>?) "
        "ORDER BY id LIMIT 96",
        (scope_id, model),
    ).fetchall()
    try:
        query_vector: list[float] | None = None
        for offset in range(0, len(missing), 31):
            batch = missing[offset : offset + 31]
            texts = [str(row["content"] or "") for row in batch]
            include_query = query_vector is None
            vectors = await llm.embeddings(
                ([query] if include_query else []) + texts,
                model=model,
                base_url=config.OPENAI_BASE_URL,
                api_key=config.OPENAI_API_KEY,
                scope_id=scope_id,
                user_id=user_id,
            )
            if include_query:
                query_vector = vectors.pop(0)
            for row, vector in zip(batch, vectors):
                c.execute(
                    "UPDATE kb_docs SET embedding=?,embedding_model=? WHERE id=? AND scope_id=?",
                    (json.dumps(vector, separators=(",", ":")), model, row["id"], scope_id),
                )
        if missing:
            c.commit()
        if query_vector is None:
            vectors = await llm.embeddings(
                [query],
                model=model,
                base_url=config.OPENAI_BASE_URL,
                api_key=config.OPENAI_API_KEY,
                scope_id=scope_id,
                user_id=user_id,
            )
            query_vector = vectors[0]
    except (LLMError, ValueError, TypeError, json.JSONDecodeError):
        _LOG.warning("semantic KB search unavailable; using local text search")
        return search(query, k=k, scope_id=scope_id)

    ranked: list[tuple[float, dict[typing.Any, typing.Any]]] = []
    rows = c.execute(
        "SELECT topic,title,source,content,embedding FROM kb_docs "
        "WHERE scope_id=? AND embedding_model=? AND embedding IS NOT NULL LIMIT 2000",
        (scope_id, model),
    ).fetchall()
    for row in rows:
        try:
            vector = [float(value) for value in json.loads(row["embedding"])]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        score = _cosine(query_vector, vector)
        if score > 0.0:
            item = dict(row)
            item.pop("embedding", None)
            item["rank"] = score
            ranked.append((score, item))
    ranked.sort(key=lambda value: value[0], reverse=True)
    return [item for _score, item in ranked[: max(1, min(20, int(k)))]] or search(
        query, k=k, scope_id=scope_id
    )


def count(scope_id: str) -> int:
    ensure()
    return (
        db.conn()
        .execute("SELECT COUNT(*) n FROM kb_docs WHERE scope_id=?", (scope_id,))
        .fetchone()["n"]
    )


def topics(scope_id: str) -> List[dict[typing.Any, typing.Any]]:
    ensure()
    rows = (
        db.conn()
        .execute(
            "SELECT topic, COUNT(*) n FROM kb_docs WHERE scope_id=? GROUP BY topic ORDER BY n DESC",
            (scope_id,),
        )
        .fetchall()
    )
    return [{"topic": r["topic"], "passages": r["n"]} for r in rows]


def clear(scope_id: str, topic: str | None = None) -> int:
    """Delete all passages, or just one topic. Returns rows deleted."""
    ensure()
    c = db.conn()
    if topic:
        cur = c.execute(
            "DELETE FROM kb_docs WHERE scope_id=? AND topic=?", (scope_id, topic.strip())
        )
    else:
        cur = c.execute("DELETE FROM kb_docs WHERE scope_id=?", (scope_id,))
    c.commit()
    return cur.rowcount
