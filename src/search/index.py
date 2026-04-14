"""Vector index backed by sqlite-vec.

Stores embeddings in the same SQLite database as the sync state,
avoiding a separate storage dependency. Uses cosine similarity
via sqlite-vec's vec0 virtual table.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

from src.search.chunker import Chunk

logger = logging.getLogger(__name__)


@dataclass
class SearchHit:
    """A single result from a semantic search query."""

    chunk_id: int
    doc_id: str
    vault_path: str
    content: str
    heading_path: list[str]
    distance: float

    @property
    def score(self) -> float:
        """Convert cosine distance to a 0..1 similarity score."""
        # sqlite-vec returns cosine distance in [0, 2]; similarity = 1 - distance/2
        return max(0.0, 1.0 - self.distance / 2.0)

    @property
    def heading_context(self) -> str:
        return " › ".join(self.heading_path) if self.heading_path else ""


class VectorIndex:
    """Semantic search index stored in SQLite via sqlite-vec."""

    def __init__(self, db_path: str | Path, dimension: int):
        self._db_path = Path(db_path).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._dimension = dimension
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(self._db_path))
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.row_factory = sqlite3.Row
            self._conn = conn
        return self._conn

    def _ensure_schema(self) -> None:
        # Main metadata table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS vault_chunks (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id TEXT NOT NULL,
                vault_path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                heading_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                UNIQUE(doc_id, chunk_index)
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vault_chunks_doc ON vault_chunks(doc_id)
        """)

        # FTS5 full-text index for BM25 ranking. Shipped with SQLite's
        # default builds, no extra dependency. ``content=''`` keeps the
        # FTS table contentless so we own writes explicitly via the same
        # chunk_id used as rowid.
        self.conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vault_chunks_fts
            USING fts5(content, heading, tokenize = 'porter unicode61')
        """)

        # Check if vec0 table exists and whether dimensions match
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vault_embeddings'"
        )
        existing = cur.fetchone()

        if existing:
            try:
                cur = self.conn.execute("SELECT embedding FROM vault_embeddings LIMIT 1")
                row = cur.fetchone()
                if row is not None:
                    stored_dim = len(row[0]) // 4  # 4 bytes per float32
                    if stored_dim != self._dimension:
                        logger.warning(
                            "Existing embeddings have dimension %d, expected %d. "
                            "Rebuilding index. Run `remark-bridge reindex` to populate.",
                            stored_dim, self._dimension,
                        )
                        self.conn.execute("DROP TABLE vault_embeddings")
                        self.conn.execute("DELETE FROM vault_chunks")
                        existing = None
            except sqlite3.OperationalError:
                pass

        if not existing:
            self.conn.execute(
                f"CREATE VIRTUAL TABLE vault_embeddings USING vec0("
                f"chunk_id INTEGER PRIMARY KEY, "
                f"embedding FLOAT[{self._dimension}])"
            )

        self.conn.commit()

    def upsert_document(
        self,
        doc_id: str,
        vault_path: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> None:
        """Insert or replace all chunks for a document."""
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunk/embedding count mismatch: {len(chunks)} vs {len(embeddings)}"
            )

        self.remove_document(doc_id)

        for chunk, vector in zip(chunks, embeddings, strict=True):
            if len(vector) != self._dimension:
                raise ValueError(
                    f"Vector dimension {len(vector)} does not match index "
                    f"dimension {self._dimension}"
                )

            content_hash = _hash(chunk.content)
            cursor = self.conn.execute(
                """INSERT INTO vault_chunks
                   (doc_id, vault_path, chunk_index, content, heading_path, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (doc_id, vault_path, chunk.index, chunk.content,
                 json.dumps(chunk.heading_path), content_hash),
            )
            chunk_id = cursor.lastrowid

            packed = _pack_vector(vector)
            self.conn.execute(
                "INSERT INTO vault_embeddings (chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, packed),
            )

            # Full-text row — rowid == chunk_id so we can rejoin cleanly.
            heading_flat = " › ".join(chunk.heading_path) if chunk.heading_path else ""
            self.conn.execute(
                "INSERT INTO vault_chunks_fts (rowid, content, heading) "
                "VALUES (?, ?, ?)",
                (chunk_id, chunk.content, heading_flat),
            )

        self.conn.commit()
        logger.info("Indexed %d chunks for document %s", len(chunks), doc_id[:8])

    def remove_document(self, doc_id: str) -> int:
        """Remove all chunks for a document. Returns chunk count removed."""
        cur = self.conn.execute(
            "SELECT chunk_id FROM vault_chunks WHERE doc_id = ?", (doc_id,),
        )
        chunk_ids = [row[0] for row in cur.fetchall()]

        if not chunk_ids:
            return 0

        placeholders = ",".join(["?"] * len(chunk_ids))
        self.conn.execute(
            f"DELETE FROM vault_embeddings WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        )
        self.conn.execute(
            f"DELETE FROM vault_chunks_fts WHERE rowid IN ({placeholders})",
            chunk_ids,
        )
        self.conn.execute(
            "DELETE FROM vault_chunks WHERE doc_id = ?", (doc_id,),
        )
        self.conn.commit()
        return len(chunk_ids)

    def search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """Find the top-k most similar chunks to a query vector."""
        if len(query_vector) != self._dimension:
            raise ValueError(
                f"Query vector dim {len(query_vector)} != index dim {self._dimension}"
            )

        packed = _pack_vector(query_vector)

        cur = self.conn.execute(
            """SELECT vc.chunk_id, vc.doc_id, vc.vault_path, vc.content,
                      vc.heading_path, ve.distance
               FROM vault_embeddings ve
               JOIN vault_chunks vc ON ve.chunk_id = vc.chunk_id
               WHERE ve.embedding MATCH ?
                 AND k = ?
               ORDER BY ve.distance""",
            (packed, top_k),
        )

        hits = []
        for row in cur.fetchall():
            hit = SearchHit(
                chunk_id=row["chunk_id"],
                doc_id=row["doc_id"],
                vault_path=row["vault_path"],
                content=row["content"],
                heading_path=json.loads(row["heading_path"]),
                distance=row["distance"],
            )
            if hit.score >= min_score:
                hits.append(hit)

        return hits

    def stats(self) -> dict:
        """Return index statistics."""
        cur = self.conn.execute("""
            SELECT COUNT(*) as total_chunks,
                   COUNT(DISTINCT doc_id) as total_docs
            FROM vault_chunks
        """)
        row = cur.fetchone()
        return {
            "total_chunks": row["total_chunks"] or 0,
            "total_docs": row["total_docs"] or 0,
            "dimension": self._dimension,
        }

    def search_bm25(
        self,
        query: str,
        top_k: int = 5,
    ) -> list[SearchHit]:
        """Find the top-k chunks ranked by BM25 (FTS5 ``rank``).

        Returns hits ordered best-first. The ``distance`` field carries
        the negated BM25 score so lower is better, mirroring the vector
        API — use ``SearchHit.score`` for a normalized 0..1 relevance.
        """
        if not query.strip():
            return []

        # FTS5 MATCH has its own mini-language with operators
        # (AND/OR/NEAR/parens/quotes). Splitting the user input into
        # word-shaped tokens and OR-ing individually quoted terms lets
        # BM25 rank any-term matches while neutralising operator chars.
        import re

        tokens = [tok for tok in re.findall(r"\w+", query, flags=re.UNICODE) if tok]
        if not tokens:
            return []
        safe_query = " OR ".join(f'"{tok}"' for tok in tokens)

        cur = self.conn.execute(
            """SELECT vc.chunk_id, vc.doc_id, vc.vault_path, vc.content,
                      vc.heading_path, vf.rank AS bm25_rank
               FROM vault_chunks_fts vf
               JOIN vault_chunks vc ON vc.chunk_id = vf.rowid
               WHERE vault_chunks_fts MATCH ?
               ORDER BY vf.rank
               LIMIT ?""",
            (safe_query, top_k),
        )

        hits: list[SearchHit] = []
        for row in cur.fetchall():
            # FTS5 rank is negative; closer to 0 = less relevant, more
            # negative = more relevant. Map into a [0, 1] similarity so
            # the rest of the pipeline can treat BM25 and vectors the
            # same way. The exact scale doesn't matter for ranking, only
            # for the optional ``min_score`` floor.
            raw = row["bm25_rank"]
            normalized = max(0.0, min(1.0, -raw / 10.0)) if raw is not None else 0.0
            hits.append(SearchHit(
                chunk_id=row["chunk_id"],
                doc_id=row["doc_id"],
                vault_path=row["vault_path"],
                content=row["content"],
                heading_path=json.loads(row["heading_path"]),
                # Re-use the cosine-distance slot: 2*(1-sim) keeps
                # ``SearchHit.score`` monotonic with relevance.
                distance=2.0 * (1.0 - normalized),
            ))
        return hits

    def clear(self) -> None:
        """Remove all entries from the index."""
        self.conn.execute("DELETE FROM vault_embeddings")
        self.conn.execute("DELETE FROM vault_chunks_fts")
        self.conn.execute("DELETE FROM vault_chunks")
        self.conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


def _pack_vector(vector: list[float]) -> bytes:
    """Pack a float vector into sqlite-vec's expected bytes format."""
    return struct.pack(f"{len(vector)}f", *vector)


def _hash(text: str) -> str:
    """Short hash of chunk content for change detection."""
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
