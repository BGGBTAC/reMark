"""Document management for reMarkable Cloud.

Higher-level operations on top of the Cloud API client — handles
downloading, caching, and metadata resolution for documents.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.remarkable.cloud import DocumentMetadata, RemarkableCloud

logger = logging.getLogger(__name__)


@dataclass
class ResolvedDocument:
    """A document with its full metadata and local path after download."""

    meta: DocumentMetadata
    local_dir: Path
    folder_path: str  # e.g. "Work/Meetings" — resolved from parent chain
    page_ids: list[str] = field(default_factory=list)
    page_count: int = 0


class DocumentManager:
    """Manages document downloading, metadata resolution, and local caching."""

    def __init__(self, cloud: RemarkableCloud, download_dir: Path):
        self._cloud = cloud
        self._download_dir = download_dir
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._folder_cache: dict[str, str] = {}  # id -> name

    async def list_documents(
        self,
        sync_folders: list[str] | None = None,
        ignore_folders: list[str] | None = None,
    ) -> list[DocumentMetadata]:
        """List all documents, optionally filtered by folder.

        Args:
            sync_folders: Only include docs in these folders (empty = all).
            ignore_folders: Exclude docs in these folders.
        """
        all_items = await self._cloud.list_items()

        # Build folder lookup: id -> name
        self._folder_cache = {
            item.id: item.name for item in all_items if item.is_folder
        }

        documents = [item for item in all_items if not item.is_folder]

        if not sync_folders and not ignore_folders:
            return documents

        ignore_set = set(ignore_folders or [])
        sync_set = set(sync_folders or [])

        filtered = []
        for doc in documents:
            folder_name = self._folder_cache.get(doc.parent, "")

            if folder_name in ignore_set:
                continue

            if sync_set and folder_name not in sync_set:
                continue

            filtered.append(doc)

        logger.info(
            "Filtered %d -> %d documents (sync: %s, ignore: %s)",
            len(documents), len(filtered), sync_folders, ignore_folders,
        )
        return filtered

    async def download(self, doc: DocumentMetadata) -> ResolvedDocument:
        """Download a document and resolve its metadata.

        Returns a ResolvedDocument with the local path and parsed metadata.
        """
        local_dir = await self._cloud.download_document(doc.id, self._download_dir)

        # Read .content file for page IDs
        page_ids = self._read_page_ids(local_dir, doc.id)

        # Resolve the full folder path
        folder_path = self._resolve_folder_path(doc.parent)

        # Try to read the real name from .metadata if available
        name = self._read_doc_name(local_dir, doc.id) or doc.name

        resolved = ResolvedDocument(
            meta=DocumentMetadata(
                id=doc.id,
                name=name,
                parent=doc.parent,
                doc_type=doc.doc_type,
                version=doc.version,
                hash=doc.hash,
                modified=doc.modified,
            ),
            local_dir=local_dir,
            folder_path=folder_path,
            page_ids=page_ids,
            page_count=len(page_ids),
        )

        logger.info(
            "Downloaded '%s' (%d pages, folder: %s)",
            name, resolved.page_count, folder_path or "root",
        )
        return resolved

    def _resolve_folder_path(self, parent_id: str) -> str:
        """Walk up the parent chain to build a full folder path like 'Work/Meetings'."""
        if not parent_id or parent_id not in self._folder_cache:
            return ""

        parts: list[str] = []
        current = parent_id
        seen = set()  # guard against cycles

        while current and current in self._folder_cache and current not in seen:
            seen.add(current)
            parts.append(self._folder_cache[current])
            # Would need parent-of-parent info for nested folders,
            # which requires the full item list. For now, single level.
            break

        return "/".join(reversed(parts))

    def _read_page_ids(self, doc_dir: Path, doc_id: str) -> list[str]:
        """Read page IDs from the .content file."""
        content_file = doc_dir / f"{doc_id}.content"
        if not content_file.exists():
            # Try without doc_id prefix (depends on download format)
            for f in doc_dir.glob("*.content"):
                content_file = f
                break

        if not content_file.exists():
            logger.warning("No .content file found in %s", doc_dir)
            return []

        try:
            data = json.loads(content_file.read_text())
            pages = data.get("cPages", {}).get("pages", [])
            if pages:
                return [p.get("id", p.get("idx", "")) for p in pages if isinstance(p, dict)]
            # Older format: flat list of page UUIDs
            return data.get("pages", [])
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse .content file: %s", e)
            return []

    def _read_doc_name(self, doc_dir: Path, doc_id: str) -> str | None:
        """Read the document name from .metadata file."""
        meta_file = doc_dir / f"{doc_id}.metadata"
        if not meta_file.exists():
            for f in doc_dir.glob("*.metadata"):
                meta_file = f
                break

        if not meta_file.exists():
            return None

        try:
            data = json.loads(meta_file.read_text())
            return data.get("visibleName")
        except (json.JSONDecodeError, KeyError):
            return None

    def cleanup(self, doc_id: str) -> None:
        """Remove downloaded files for a document."""
        import shutil
        doc_dir = self._download_dir / doc_id
        if doc_dir.exists():
            shutil.rmtree(doc_dir)
            logger.debug("Cleaned up %s", doc_dir)
