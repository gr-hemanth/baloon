import io
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile, status
import docx
from pypdf import PdfReader

from packages.shared.schemas.job import WorksheetUploadResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/worksheets", tags=["Worksheets"])

MAX_UPLOAD_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB limit
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
UPLOAD_DIR = (PROJECT_ROOT / "artifacts" / "uploads" / "worksheets").resolve()

DOCX_MAGIC = b"PK\x03\x04"
PDF_MAGIC = b"%PDF-"


@router.post("/upload", response_model=WorksheetUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_worksheet(file: UploadFile = File(...)):
    """Upload and validate a user-provided worksheet (DOCX or PDF).
    
    Validates:
    - File extension (.docx, .pdf)
    - File size (<= 15 MB)
    - File magic byte signatures
    - Document structure and integrity
    - Prevents path traversal vulnerabilities
    
    Returns upload metadata including standalone upload_id and absolute stored_path.
    """
    raw_filename = file.filename or ""
    clean_filename = Path(raw_filename).name.strip()
    
    if not clean_filename or clean_filename.startswith(".") or ".." in clean_filename or "/" in clean_filename or "\\" in clean_filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or potentially malicious filename detected.",
        )

    suffix = Path(clean_filename).suffix.lower()
    if suffix not in (".docx", ".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file extension '{suffix}'. Only .docx and .pdf files are supported.",
        )

    # Read uploaded bytes with size validation
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum allowed size of 15 MB (received {len(content)} bytes).",
        )

    # Magic byte and document integrity validation
    if suffix == ".docx":
        if not content.startswith(DOCX_MAGIC):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid DOCX file: magic byte signature mismatch (expected PK\\x03\\x04).",
            )
        try:
            doc_stream = io.BytesIO(content)
            docx.Document(doc_stream)
        except Exception as doc_err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Corrupted or invalid DOCX document: {doc_err}",
            )
        fmt = "docx"

    elif suffix == ".pdf":
        if not content.startswith(PDF_MAGIC):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid PDF file: magic byte signature mismatch (expected %PDF-).",
            )
        try:
            pdf_stream = io.BytesIO(content)
            reader = PdfReader(pdf_stream)
            if len(reader.pages) == 0:
                raise ValueError("PDF document contains 0 pages.")
        except Exception as pdf_err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Corrupted or invalid PDF document: {pdf_err}",
            )
        fmt = "pdf"
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format '{suffix}'.",
        )

    # Sanitize stored filename and prevent path traversal
    upload_id = str(uuid.uuid4())
    safe_stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", Path(clean_filename).stem)
    stored_filename = f"{upload_id}_{safe_stem}{suffix}"
    
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target_path = (UPLOAD_DIR / stored_filename).resolve()

    try:
        if not target_path.is_relative_to(UPLOAD_DIR):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Path traversal attempt detected.",
            )
    except AttributeError:
        # Fallback for older python or complex paths
        if not str(target_path).startswith(str(UPLOAD_DIR)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Path traversal attempt detected.",
            )

    target_path.write_bytes(content)
    logger.info("Successfully stored user-provided worksheet at %s (%d bytes)", target_path, len(content))

    return WorksheetUploadResponse(
        upload_id=upload_id,
        original_filename=clean_filename,
        stored_path=str(target_path),
        file_size=len(content),
        format=fmt,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
