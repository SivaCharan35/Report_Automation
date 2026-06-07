"""
Azure Blob Storage upload utilities.
Only imported when UPLOAD_AZURE=True — azure-storage-blob need not be installed
if Azure is not being used.
"""

from __future__ import annotations

import logging
from pathlib import Path

from azure.storage.blob import BlobServiceClient, ContentSettings

import config

logger = logging.getLogger(__name__)


def _client(blob_name: str):
    svc = BlobServiceClient.from_connection_string(config.AZURE_CONNECTION_STRING)
    return svc.get_blob_client(container=config.AZURE_CONTAINER_NAME, blob=blob_name)


def upload_file_to_azure(file_path: Path, blob_name: str) -> str:
    """Upload a local file. Returns the public blob URL."""
    bc = _client(blob_name)
    with open(file_path, "rb") as f:
        bc.upload_blob(f, overwrite=True)
    logger.info("Azure ↑  %s → %s", file_path.name, bc.url)
    return bc.url


def upload_bytes_to_azure(
    data: bytes,
    blob_name: str,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload raw bytes. Returns the public blob URL."""
    bc = _client(blob_name)
    bc.upload_blob(
        data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )
    logger.info("Azure ↑  bytes → %s", bc.url)
    return bc.url
