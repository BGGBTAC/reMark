"""reMarkable Cloud API client (sync 1.5 protocol).

All communication with the reMarkable Cloud goes through this module.
Never call these endpoints directly from other parts of the codebase.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from src.remarkable.auth import AuthManager

logger = logging.getLogger(__name__)

SERVICE_MANAGER_URL = (
    "https://service-manager-production-dot-remarkable-production.appspot.com"
    "/service/json/1/{service}?environment=production&group=auth0|{user_id}&apiVer=2"
)

# Retry config
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds


@dataclass
class DocumentMetadata:
    """Metadata for a single document on the reMarkable Cloud."""

    id: str
    name: str
    parent: str
    doc_type: str  # "DocumentType" or "CollectionType" (folder)
    version: int
    hash: str
    modified: str
    current_page: int = 0
    bookmarked: bool = False

    @property
    def is_folder(self) -> bool:
        return self.doc_type == "CollectionType"


@dataclass
class SyncRoot:
    """Parsed sync 1.5 root index."""

    generation: int
    hash: str
    schema_version: int = 3
    files: list[dict[str, Any]] = field(default_factory=list)


class CloudError(Exception):
    """Raised when a Cloud API call fails."""


class RemarkableCloud:
    """Async client for the reMarkable Cloud sync 1.5 protocol.

    Uses connection pooling via httpx.AsyncClient and automatically
    refreshes auth tokens when needed.
    """

    def __init__(self, auth: AuthManager):
        self._auth = auth
        self._storage_host: str | None = None
        self._notifications_host: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> RemarkableCloud:
        self._client = httpx.AsyncClient(timeout=60, follow_redirects=True)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise CloudError("Use 'async with RemarkableCloud(auth)' as context manager")
        return self._client

    async def _auth_headers(self) -> dict[str, str]:
        token = await self._auth.get_user_token()
        return {"Authorization": f"Bearer {token}"}

    async def discover_storage_host(self) -> str:
        """Discover the storage service endpoint. Cached after first call."""
        if self._storage_host:
            return self._storage_host

        headers = await self._auth_headers()
        url = SERVICE_MANAGER_URL.format(
            service="document-storage",
            user_id="",  # extracted from JWT by the server
        )

        resp = await self._request("GET", url, headers=headers)
        data = resp.json()
        host = data.get("Host", "")
        if not host:
            raise CloudError(f"No storage host in service discovery response: {data}")

        self._storage_host = f"https://{host}"
        logger.info("Storage host: %s", self._storage_host)
        return self._storage_host

    async def get_notifications_host(self) -> str:
        """Discover the WebSocket notifications endpoint."""
        if self._notifications_host:
            return self._notifications_host

        headers = await self._auth_headers()
        url = SERVICE_MANAGER_URL.format(
            service="notifications",
            user_id="",
        )

        resp = await self._request("GET", url, headers=headers)
        data = resp.json()
        host = data.get("Host", "")
        if not host:
            raise CloudError(f"No notifications host in discovery response: {data}")

        self._notifications_host = f"wss://{host}"
        logger.info("Notifications host: %s", self._notifications_host)
        return self._notifications_host

    async def list_items(self) -> list[DocumentMetadata]:
        """Fetch the full document tree from the Cloud.

        Uses the sync 1.5 root index to enumerate all documents and folders.
        """
        storage = await self.discover_storage_host()
        headers = await self._auth_headers()

        # Get the root index
        root_url = f"{storage}/sync/v2/root"
        resp = await self._request("GET", root_url, headers=headers)
        root_hash = resp.text.strip()

        if not root_hash:
            logger.info("Empty root index — no documents on cloud")
            return []

        # Fetch the root index blob
        index = await self._fetch_blob(storage, root_hash, headers)
        root_data = self._parse_root_index(index)

        # Parse each file entry into DocumentMetadata
        docs = []
        for entry in root_data.files:
            try:
                doc = self._parse_document_entry(entry)
                docs.append(doc)
            except (KeyError, ValueError) as e:
                logger.warning("Skipping malformed entry: %s", e)

        logger.info("Listed %d items from cloud (%d folders)", len(docs), sum(1 for d in docs if d.is_folder))
        return docs

    async def download_document(self, doc_id: str, target_dir: Path) -> Path:
        """Download all blobs for a document and reconstruct local file structure.

        Returns the path to the downloaded document directory.
        """
        storage = await self.discover_storage_host()
        headers = await self._auth_headers()

        doc_dir = target_dir / doc_id
        doc_dir.mkdir(parents=True, exist_ok=True)

        # Get the document index (lists all blobs for this doc)
        doc_index_url = f"{storage}/sync/v2/signed-urls/downloads"
        payload = {"relative_path": doc_id, "http_method": "GET"}
        resp = await self._request("PUT", doc_index_url, headers=headers, json=payload)

        if resp.status_code == 200:
            url_data = resp.json()
            download_url = url_data.get("url", "")
            if download_url:
                blob_resp = await self._request("GET", download_url)
                # The response is a zip-like bundle of all document files
                await self._extract_document_bundle(blob_resp.content, doc_dir)
        else:
            # Fallback: try fetching individual blobs
            await self._download_document_blobs(storage, doc_id, doc_dir, headers)

        logger.info("Downloaded document %s to %s", doc_id[:8], doc_dir)
        return doc_dir

    async def upload_document(self, source_path: Path, parent_folder: str = "") -> str:
        """Upload a PDF or EPUB to the reMarkable Cloud.

        Returns the new document UUID.
        """
        storage = await self.discover_storage_host()
        headers = await self._auth_headers()

        doc_id = str(uuid4())
        doc_name = source_path.stem

        # Create the upload request
        upload_url = f"{storage}/sync/v2/signed-urls/uploads"
        payload = {
            "relative_path": doc_id,
            "http_method": "PUT",
        }
        resp = await self._request("PUT", upload_url, headers=headers, json=payload)
        url_data = resp.json()
        signed_url = url_data.get("url", "")

        if not signed_url:
            raise CloudError(f"No upload URL returned for document {doc_id}")

        # Read the file and upload
        content = source_path.read_bytes()
        await self._request("PUT", signed_url, content=content)

        # Create metadata entry
        metadata = {
            "ID": doc_id,
            "Type": "DocumentType",
            "VissibleName": doc_name,  # yes, reMarkable misspells this
            "Parent": parent_folder,
            "Version": 1,
            "CurrentPage": 0,
        }

        meta_url = f"{storage}/sync/v2/metadata"
        await self._request("PUT", meta_url, headers=headers, json=[metadata])

        logger.info("Uploaded %s as %s (parent: %s)", source_path.name, doc_id[:8], parent_folder or "root")
        return doc_id

    async def create_folder(self, name: str, parent: str = "") -> str:
        """Create a new folder (CollectionType) on the Cloud.

        Returns the folder UUID.
        """
        storage = await self.discover_storage_host()
        headers = await self._auth_headers()

        folder_id = str(uuid4())
        metadata = {
            "ID": folder_id,
            "Type": "CollectionType",
            "VissibleName": name,
            "Parent": parent,
            "Version": 1,
        }

        meta_url = f"{storage}/sync/v2/metadata"
        await self._request("PUT", meta_url, headers=headers, json=[metadata])

        logger.info("Created folder '%s' (%s)", name, folder_id[:8])
        return folder_id

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        """Make an HTTP request with retry logic.

        Retries on 429 and 5xx with exponential backoff.
        """
        import asyncio

        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                resp = await self.client.request(
                    method, url, headers=headers, json=json, content=content,
                )

                if resp.status_code < 400:
                    return resp

                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = RETRY_BACKOFF_BASE ** attempt
                    logger.warning(
                        "Request %s %s returned %d, retrying in %ds (attempt %d/%d)",
                        method, _redact_url(url), resp.status_code, wait, attempt + 1, MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue

                # 4xx (not 429) — don't retry
                raise CloudError(
                    f"{method} {_redact_url(url)} failed: HTTP {resp.status_code}"
                )

            except httpx.TransportError as e:
                last_error = e
                wait = RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "Transport error on %s %s: %s, retrying in %ds",
                    method, _redact_url(url), e, wait,
                )
                import asyncio
                await asyncio.sleep(wait)

        raise CloudError(f"Request failed after {MAX_RETRIES} retries: {last_error}")

    async def _fetch_blob(
        self, storage: str, blob_hash: str, headers: dict[str, str]
    ) -> bytes:
        """Fetch a single blob by its hash."""
        url = f"{storage}/sync/v2/signed-urls/downloads"
        payload = {"relative_path": blob_hash, "http_method": "GET"}
        resp = await self._request("PUT", url, headers=headers, json=payload)
        url_data = resp.json()
        signed_url = url_data.get("url", "")

        if not signed_url:
            raise CloudError(f"No download URL for blob {blob_hash[:12]}")

        blob_resp = await self._request("GET", signed_url)
        return blob_resp.content

    def _parse_root_index(self, data: bytes) -> SyncRoot:
        """Parse the sync 1.5 root index format.

        The root index is a newline-separated list of entries.
        Each entry has the format: hash:type:id:subfiles:size:timestamp
        """
        root = SyncRoot(generation=0, hash="")
        lines = data.decode("utf-8", errors="replace").strip().split("\n")

        if not lines or not lines[0]:
            return root

        # First line is the schema version / generation
        try:
            root.schema_version = int(lines[0])
        except ValueError:
            pass

        for line in lines[1:]:
            if not line.strip():
                continue
            parts = line.split(":")
            if len(parts) >= 3:
                root.files.append({
                    "hash": parts[0],
                    "type": parts[1],
                    "id": parts[2],
                    "subfiles": int(parts[3]) if len(parts) > 3 else 0,
                    "size": int(parts[4]) if len(parts) > 4 else 0,
                    "modified": parts[5] if len(parts) > 5 else "",
                })

        return root

    def _parse_document_entry(self, entry: dict[str, Any]) -> DocumentMetadata:
        """Parse a root index entry into DocumentMetadata.

        Full metadata requires fetching the .metadata file for each doc,
        but the index gives us enough for sync decisions.
        """
        return DocumentMetadata(
            id=entry["id"],
            name=entry.get("name", entry["id"]),
            parent=entry.get("parent", ""),
            doc_type=entry.get("type", "DocumentType"),
            version=entry.get("subfiles", 0),
            hash=entry["hash"],
            modified=entry.get("modified", ""),
        )

    async def _download_document_blobs(
        self, storage: str, doc_id: str, doc_dir: Path, headers: dict[str, str]
    ) -> None:
        """Download individual blobs for a document when bundle download is unavailable."""
        # Fetch the document's file index
        index_data = await self._fetch_blob(storage, doc_id, headers)
        lines = index_data.decode("utf-8", errors="replace").strip().split("\n")

        for line in lines:
            if not line.strip():
                continue
            parts = line.split(":")
            if len(parts) < 2:
                continue

            blob_hash = parts[0]
            blob_name = parts[1] if len(parts) > 1 else blob_hash

            blob_data = await self._fetch_blob(storage, blob_hash, headers)
            blob_path = doc_dir / blob_name
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            blob_path.write_bytes(blob_data)

    async def _extract_document_bundle(self, data: bytes, target_dir: Path) -> None:
        """Extract a downloaded document bundle into the target directory.

        The bundle is typically a zip-format archive containing
        all files for a single document (.rm, .metadata, .content, .pagedata, etc.)
        """
        import io
        import zipfile

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(target_dir)
        except zipfile.BadZipFile:
            # Not a zip — might be a raw blob, save as-is
            (target_dir / "raw_content").write_bytes(data)
            logger.warning("Document bundle is not a zip, saved as raw_content")


def _redact_url(url: str) -> str:
    """Remove query params (which may contain tokens) for logging."""
    return url.split("?")[0] if "?" in url else url
