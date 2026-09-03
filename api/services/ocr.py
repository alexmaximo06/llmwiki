"""Routes uploaded documents to the right extraction backend and stores the results."""

import asyncio
import logging
import tempfile
from pathlib import Path

import asyncpg
from config import settings
from domain.file_types import IMAGE_TYPES, OFFICE_TYPES
from html_parser import Parser
from services.chunker import chunk_text
from services.converter_client import ConverterClient
from services.mistral_ocr_client import MistralOcrClient
from services.office_local import convert_office_to_pdf
from services.page_quota import PageQuota
from services.page_store import PageStore
from services.s3 import S3Service
from services.spreadsheet_extract import parse_sheets

logger = logging.getLogger(__name__)


def _extract_pdf_in_process(pdf_path: str):
    """Lazy local-only import; hosted mode must never load the PDF parser."""
    from services.pdf_extract import extract_pdf

    return extract_pdf(pdf_path)


class OCRService:
    PROCESS_CONCURRENCY = 3

    def __init__(self, s3: S3Service, pool: asyncpg.Pool):
        self._s3 = s3
        self._pool = pool
        self._semaphore = asyncio.Semaphore(self.PROCESS_CONCURRENCY)
        self._quota = PageQuota(pool)
        self._pages = PageStore(pool, s3, self._quota)
        self._converter = ConverterClient()
        self._mistral = MistralOcrClient()

    async def process_document(self, document_id: str, user_id: str):
        async with self._semaphore:
            await self._do_process(document_id, user_id)

    async def _do_process(self, document_id: str, user_id: str):
        try:
            await self._quota.check_global_limits()
            await self._pages.set_status(document_id, "processing")

            doc = await self._fetch_document(document_id, user_id)
            if not doc:
                logger.error("Document %s not found for user %s", document_id, user_id)
                return

            ext = doc["filename"].rsplit(".", 1)[-1].lower() if "." in doc["filename"] else doc["file_type"]
            kb_id = doc["kb_id"]
            s3_source_key = f"{user_id}/{document_id}/source.{ext}"

            if ext in OFFICE_TYPES:
                await self._process_office(document_id, user_id, kb_id, s3_source_key, ext)
            elif ext in IMAGE_TYPES:
                await self._pages.mark_image_ready(document_id, user_id)
            elif ext == "pdf":
                await self._process_pdf(document_id, user_id, kb_id, s3_source_key)
            elif ext in ("html", "htm"):
                await self._process_html(document_id, user_id, kb_id, s3_source_key)
            elif ext in ("xlsx", "xls", "csv"):
                await self._process_spreadsheet(document_id, user_id, kb_id, s3_source_key, ext)
            else:
                raise ValueError(f"Unsupported file type: {ext}")

        except Exception as e:
            logger.exception("Processing failed for document %s", document_id)
            await self._mark_failed(document_id, str(e)[:500])

    # ── PDF extraction ────────────────────────────────────────────────────

    async def _process_pdf(self, document_id: str, user_id: str, kb_id: str, s3_source_key: str):
        if settings.PDF_BACKEND == "mistral":
            if not settings.MISTRAL_API_KEY:
                raise ValueError("MISTRAL_API_KEY not configured — cannot process PDFs")
            # Cheap pre-check: refuse if user is already over quota so we don't burn the Mistral call.
            await self._quota.check_user_page_limit(user_id, 1)
            presigned_url = await self._s3.generate_presigned_get(s3_source_key)
            ocr_result = await self._mistral.ocr(presigned_url)
            await self._pages.store_mistral_result(document_id, user_id, kb_id, ocr_result)
        elif settings.CONVERTER_URL:
            presigned_url = await self._s3.generate_presigned_get(s3_source_key)
            extraction = await self._converter.extract(presigned_url, "pdf")
            await self._pages.store_extracted_pages(
                document_id, user_id, kb_id, extraction.pages, extraction.engine,
            )
        else:
            if settings.MODE != "local":
                raise RuntimeError(
                    "Hosted PDF parsing requires the isolated converter service"
                )
            await self._process_opendataloader(document_id, user_id, kb_id, s3_source_key)

    async def _process_office(self, document_id: str, user_id: str, kb_id: str, s3_source_key: str, ext: str):
        """Office files go through the converter when available; local mode falls back to LibreOffice."""
        if settings.CONVERTER_URL:
            presigned_url = await self._s3.generate_presigned_get(s3_source_key)
            extraction = await self._converter.extract(presigned_url, ext)
            await self._pages.store_extracted_pages(
                document_id, user_id, kb_id, extraction.pages, f"libreoffice+{extraction.engine}",
            )
            return
        if settings.MODE != "local":
            raise RuntimeError(
                "Hosted Office parsing requires the isolated converter service"
            )
        if settings.PDF_BACKEND == "mistral":
            if not settings.MISTRAL_API_KEY:
                raise ValueError("MISTRAL_API_KEY not configured")
            pdf_key = await self._convert_to_pdf_s3(document_id, user_id, s3_source_key, ext)
            await self._quota.check_user_page_limit(user_id, 1)
            presigned_url = await self._s3.generate_presigned_get(pdf_key)
            ocr_result = await self._mistral.ocr(presigned_url)
            await self._pages.store_mistral_result(document_id, user_id, kb_id, ocr_result)
            return
        await self._process_office_local(document_id, user_id, kb_id, s3_source_key, ext)

    # ── OpenDataLoader local extraction ───────────────────────────────────

    async def _process_opendataloader(self, document_id: str, user_id: str, kb_id: str, s3_source_key: str):
        """Extract PDF via opendataloader-pdf in local mode only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "source.pdf"
            await self._s3.download_to_file(s3_source_key, str(pdf_path))
            pages_with_images = await asyncio.to_thread(
                _extract_pdf_in_process,
                str(pdf_path),
            )

        await self._store_local_extraction(
            document_id, user_id, kb_id, pages_with_images, "opendataloader",
        )

    # ── Office local fallback (no converter) ──────────────────────────────

    async def _convert_to_pdf_s3(self, document_id: str, user_id: str, s3_source_key: str, ext: str) -> str:
        """Convert an Office file to PDF locally and upload it. Returns the PDF's S3 key."""
        if settings.MODE != "local":
            raise RuntimeError(
                "Hosted Office conversion requires the isolated converter service"
            )
        pdf_key = f"{user_id}/{document_id}/converted.pdf"
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / f"source.{ext}"
            await self._s3.download_to_file(s3_source_key, str(source_path))
            pdf_path = await convert_office_to_pdf(source_path, tmpdir)
            await self._s3.upload_file(pdf_key, str(pdf_path), "application/pdf")
        return pdf_key

    async def _process_office_local(self, document_id: str, user_id: str, kb_id: str, s3_source_key: str, ext: str):
        """Convert an Office file to PDF locally, then extract with opendataloader."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / f"source.{ext}"
            await self._s3.download_to_file(s3_source_key, str(source_path))
            pdf_path = await convert_office_to_pdf(source_path, tmpdir)
            pages_with_images = await asyncio.to_thread(
                _extract_pdf_in_process,
                str(pdf_path),
            )

        await self._store_local_extraction(
            document_id, user_id, kb_id, pages_with_images, "libreoffice+opendataloader",
        )

    async def _store_local_extraction(
        self, document_id: str, user_id: str, kb_id: str,
        pages_with_images: list[tuple[int, str, list[dict]]], parser: str,
    ):
        assets, page_elements = await self._pages.build_pdf_assets(document_id, pages_with_images)
        page_contents = [(num, md) for num, md, _ in pages_with_images]
        await self._pages.store_extracted_pages(
            document_id, user_id, kb_id, page_contents, parser,
            page_elements=page_elements, assets=assets,
        )
        await self._pages.upload_assets(user_id, assets)

    # ── HTML processing ───────────────────────────────────────────────────

    async def _process_html(self, document_id: str, user_id: str, kb_id: str, s3_source_key: str):
        """Parse HTML with the webmd parser, store markdown + tagged HTML."""
        html_bytes = await self._s3.download_bytes(s3_source_key)
        raw_html = html_bytes.decode("utf-8", errors="replace")

        parser = Parser(raw_html, content_only=True)
        result = parser.parse()

        await parser.embed_images()
        tagged_html = parser.html(sanitize=True)
        await self._s3.upload_bytes(
            f"{user_id}/{document_id}/tagged.html",
            tagged_html.encode("utf-8"),
            "text/html",
        )

        chunks = chunk_text(result.content)
        await self._pages.store_html(document_id, user_id, kb_id, result.content, chunks)

    # ── Spreadsheet processing ────────────────────────────────────────────

    async def _process_spreadsheet(self, document_id: str, user_id: str, kb_id: str, s3_source_key: str, ext: str):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / f"source.{ext}"
            await self._s3.download_to_file(s3_source_key, str(source_path))
            sheets = await asyncio.to_thread(parse_sheets, str(source_path), ext)

        await self._pages.store_sheets(document_id, user_id, kb_id, sheets)

    # ── Queries ───────────────────────────────────────────────────────────

    async def _fetch_document(self, document_id: str, user_id: str) -> asyncpg.Record | None:
        return await self._pool.fetchrow(
            "SELECT filename, file_type, path, knowledge_base_id::text as kb_id "
            "FROM documents WHERE id = $1 AND user_id = $2",
            document_id, user_id,
        )

    async def _mark_failed(self, document_id: str, error_message: str) -> None:
        try:
            await self._pool.execute(
                "UPDATE documents SET status = 'failed', error_message = $2, updated_at = now() "
                "WHERE id = $1",
                document_id, error_message,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to update status to failed for %s", document_id)
