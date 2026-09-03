"""Local-mode Office→PDF conversion; hosted mode uses the isolated converter service."""

import asyncio
import subprocess
from pathlib import Path

CONVERT_TIMEOUT_SECONDS = 120


async def convert_office_to_pdf(source_path: Path, tmpdir: str) -> Path:
    result = await asyncio.to_thread(
        subprocess.run,
        ["libreoffice", "--headless", "--norestore", "--convert-to", "pdf", "--outdir", tmpdir, str(source_path)],
        capture_output=True, timeout=CONVERT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed: {result.stderr.decode()[:300]}")
    pdf_path = Path(tmpdir) / f"{source_path.stem}.pdf"
    if not pdf_path.exists():
        raise RuntimeError("LibreOffice did not produce a PDF")
    return pdf_path
