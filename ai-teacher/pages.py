"""
pages.py — the file-to-page pipeline for the AI Study Room.

Takes the files saved in a session folder and converts them, file by file,
into a single ordered list of "pages" (each page is an image or a text
chunk). Pages are appended to a manifest.json as soon as they are produced,
so the lesson can start on page 1 while later files are still processing.

Session folder layout
---------------------
    uploads/<session_id>/
        originals/            sanitized uploaded files (never served raw)
        pages/page_000.png    processed page content (served via /files/)
        pages/page_003.txt
        manifest.json         ordered page list + status (atomic writes)
        upload_order.json     [{stored, original}] in upload order
        _initial_text.txt     optional free-text context from the student
"""

import base64
import json
import logging
import os
import re
import threading

import pymupdf
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

logger = logging.getLogger("GeminiLiveApp.pages")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Single source of truth for accepted upload extensions. app.py mirrors this
# for request validation; the browser mirrors it for client-side validation.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TEXT_EXTENSIONS = {".txt", ".md"}
DOC_EXTENSIONS = {".pdf", ".docx"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | TEXT_EXTENSIONS | DOC_EXTENSIONS

# Text chunking rule (documented in README):
#   - DOCX: split on top-level headings (Heading 1 / Title) when present,
#     each section further capped at DOCX_SECTION_MAX_CHARS; if the document
#     has no headings at all, fall back to plain character chunking.
#   - TXT/MD: chunk at TEXT_MAX_CHARS on paragraph boundaries.
TEXT_MAX_CHARS = 800
DOCX_SECTION_MAX_CHARS = 1600

MANIFEST_NAME = "manifest.json"
ORDER_NAME = "upload_order.json"
INITIAL_TEXT_NAME = "_initial_text.txt"

# Minimal 1x1 transparent PNG used only by unit tests.
TEST_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBg"
    "AAAABQABh6FO1AAAAABJRU5ErkJggg=="
)

# Lock serializing manifest reads-modify-writes across threads.
_MANIFEST_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def session_dir(upload_root, session_id):
    return os.path.join(upload_root, session_id)


def _atomic_write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# Text chunking
# --------------------------------------------------------------------------

def chunk_text(text, max_chars=TEXT_MAX_CHARS):
    """Split plain text into page-sized chunks on paragraph boundaries.

    Paragraphs are accumulated until adding the next one would exceed
    ``max_chars``; a single paragraph longer than ``max_chars`` is split on
    word boundaries. Whitespace-only input yields no chunks.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    chunks = []
    current = []

    def current_len():
        return sum(len(p) for p in current) + max(0, len(current) - 1)

    for para in paragraphs:
        if len(para) > max_chars:
            # Flush whatever we have, then hard-split the long paragraph.
            if current:
                chunks.append("\n\n".join(current))
                current = []
            words = para.split(" ")
            line = ""
            for word in words:
                candidate = word if not line else line + " " + word
                if len(candidate) > max_chars and line:
                    chunks.append(line)
                    line = word
                else:
                    line = candidate
            if line:
                chunks.append(line)
            continue

        if current_len() + len(para) + (1 if current else 0) > max_chars and current:
            chunks.append("\n\n".join(current))
            current = []
        current.append(para)

    if current:
        chunks.append("\n\n".join(current))
    return chunks


# --------------------------------------------------------------------------
# DOCX extraction (paragraphs AND tables, in document order)
# --------------------------------------------------------------------------

def _docx_blocks(path):
    """Yield (kind, style_name, text) for every paragraph/table in order."""
    doc = Document(path)
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            style = para.style.name if para.style is not None else ""
            if para.text.strip():
                yield ("p", style, para.text.rstrip())
        elif tag == "tbl":
            table = Table(child, doc)
            rows = []
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                rows.append(" | ".join(cells))
            if rows:
                yield ("tbl", "", "\n".join(rows))


def docx_sections(path):
    """Split a DOCX into (heading, text) sections on top-level headings."""
    sections = []
    current_heading = None
    current_lines = []
    saw_heading = False

    for kind, style, text in _docx_blocks(path):
        is_heading = kind == "p" and (style == "Heading 1" or style == "Title")
        if is_heading:
            saw_heading = True
            if current_lines:
                sections.append((current_heading, "\n\n".join(current_lines)))
            current_heading = text.strip()
            current_lines = []
        else:
            current_lines.append(text)

    if current_lines:
        sections.append((current_heading, "\n\n".join(current_lines)))

    # A lone document title with no body still deserves a page.
    if not sections and saw_heading and current_heading:
        sections.append((current_heading, ""))
    return sections, saw_heading


# --------------------------------------------------------------------------
# File handlers — each yields raw pages for one uploaded file
# --------------------------------------------------------------------------

def handle_image(path, display_name):
    """A photo/screenshot is a single image page, passed through as-is."""
    ext = os.path.splitext(path)[1].lower().replace(".", "")
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
    }.get(ext, "application/octet-stream")
    with open(path, "rb") as f:
        data = f.read()
    yield {"kind": "image", "data": data, "mime_type": mime,
           "page_in_source": 1, "label": display_name,
           "file_ext": ext}


def handle_pdf(path, display_name):
    """Every page of the PDF is rasterized to a PNG — not just page 1."""
    pdf = pymupdf.open(path)
    try:
        for number, page in enumerate(pdf, start=1):
            try:
                pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                yield {"kind": "image", "data": pix.tobytes("png"),
                       "mime_type": "image/png", "page_in_source": number,
                       "label": f"{display_name} · p.{number}",
                       "file_ext": "png"}
            except Exception:
                logger.exception("Failed rasterizing page %d of %s",
                                 number, display_name)
    finally:
        pdf.close()


def handle_docx(path, display_name):
    """DOCX → heading-based sections, chunked to slide-sized text pages."""
    sections, saw_heading = docx_sections(path)
    if not saw_heading:
        text = "\n\n".join(body for _h, body in sections if body.strip())
        for i, chunk in enumerate(chunk_text(text), start=1):
            yield {"kind": "text", "data": chunk, "mime_type": "text/plain",
                   "page_in_source": i,
                   "label": f"{display_name} · part {i}",
                   "file_ext": "txt"}
        return

    counter = 0
    for heading, body in sections:
        body = body.strip()
        pieces = chunk_text(body, max_chars=DOCX_SECTION_MAX_CHARS) if body else [""]
        for i, piece in enumerate(pieces, start=1):
            counter += 1
            content = (heading + "\n\n" + piece).strip() if heading else piece
            part = "" if len(pieces) == 1 else f", part {i}"
            yield {"kind": "text", "data": content,
                   "mime_type": "text/plain", "page_in_source": counter,
                   "label": f"{display_name} · {heading}{part}".rstrip(),
                   "file_ext": "txt"}


def handle_txt(path, display_name):
    """TXT/MD → ~800-char chunks on paragraph boundaries."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    for i, chunk in enumerate(chunk_text(text), start=1):
        yield {"kind": "text", "data": chunk, "mime_type": "text/plain",
               "page_in_source": i, "label": f"{display_name} · part {i}",
               "file_ext": "txt"}


# Registry keyed by extension. Adding .pptx later means writing one handler
# function and registering it here (see README "Extension points").
PAGE_HANDLERS = {
    ".pdf": handle_pdf,
    ".docx": handle_docx,
    ".txt": handle_txt,
    ".md": handle_txt,
}
for _ext in IMAGE_EXTENSIONS:
    PAGE_HANDLERS[_ext] = handle_image


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def init_manifest(session_directory):
    """Create the initial (processing, empty) manifest for a session."""
    path = os.path.join(session_directory, MANIFEST_NAME)
    with _MANIFEST_LOCK:
        _atomic_write_json(path, {
            "status": "processing",
            "error": None,
            "warnings": [],
            "pages": [],
            "total_pages": 0,
        })


def get_manifest(upload_root, session_id):
    """Read the current manifest for a session (None if it doesn't exist)."""
    return read_json(os.path.join(session_dir(upload_root, session_id),
                                  MANIFEST_NAME))


def _append_pages(session_directory, new_pages):
    """Atomically append fully-written page files to the manifest."""
    path = os.path.join(session_directory, MANIFEST_NAME)
    with _MANIFEST_LOCK:
        manifest = read_json(path) or {
            "status": "processing", "error": None, "warnings": [], "pages": [],
            "total_pages": 0,
        }
        manifest["pages"].extend(new_pages)
        manifest["total_pages"] = len(manifest["pages"])
        _atomic_write_json(path, manifest)


def _finish_manifest(session_directory, status, error=None, warnings=None):
    path = os.path.join(session_directory, MANIFEST_NAME)
    with _MANIFEST_LOCK:
        manifest = read_json(path) or {
            "status": status, "error": error, "warnings": warnings or [],
            "pages": [], "total_pages": 0,
        }
        manifest["status"] = status
        manifest["error"] = error
        if warnings is not None:
            manifest["warnings"] = warnings
        _atomic_write_json(path, manifest)


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------

def run_pipeline(session_directory):
    """Convert every uploaded file in a session into ordered pages.

    Designed to run in a background thread right after upload. Writes each
    page's content into pages/page_NNN.<ext> and appends it to the manifest
    immediately, so page 1 becomes available (and teachable) before the rest
    of the files finish processing.
    """
    logger.info("Pipeline started for %s", session_directory)
    order = read_json(os.path.join(session_directory, ORDER_NAME)) or []
    warnings = []
    pages_dir = os.path.join(session_directory, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    try:
        next_index = 0  # only the pipeline thread writes pages — safe
        for entry in order:
            stored_name = entry["stored"]
            display_name = entry.get("original") or stored_name
            source_path = os.path.join(session_directory, "originals",
                                       stored_name)
            if not os.path.isfile(source_path):
                warnings.append(f"{display_name}: file missing, skipped")
                continue

            ext = os.path.splitext(stored_name)[1].lower()
            handler = PAGE_HANDLERS.get(ext)
            if handler is None:
                warnings.append(f"{display_name}: unsupported type, skipped")
                continue

            try:
                pages_from_file = []
                for raw in handler(source_path, display_name):
                    index = next_index
                    next_index += 1
                    filename = f"page_{index:03d}.{raw['file_ext']}"
                    page_path = os.path.join(pages_dir, filename)
                    mode = "wb" if raw["kind"] == "image" else "w"
                    kwargs = {"encoding": "utf-8"} if mode == "w" else {}
                    with open(page_path, mode, **kwargs) as f:
                        f.write(raw["data"])
                    pages_from_file.append({
                        "index": index,
                        "kind": raw["kind"],
                        "source_file": display_name,
                        "page_in_source": raw["page_in_source"],
                        "label": raw["label"],
                        "file": f"pages/{filename}",
                        "mime_type": raw["mime_type"],
                        "chars": len(raw["data"]) if raw["kind"] == "text"
                                 else None,
                    })
                if pages_from_file:
                    _append_pages(session_directory, pages_from_file)
                    logger.info("Processed %s -> %d pages",
                                stored_name, len(pages_from_file))
                else:
                    warnings.append(f"{display_name}: no readable content")
            except Exception as exc:  # one bad file never kills the pipeline
                logger.exception("Failed processing %s", stored_name)
                warnings.append(f"{display_name}: {type(exc).__name__} — "
                                f"could not be processed")

        _finish_manifest(session_directory, "ready", None, warnings)
        logger.info("Pipeline finished for %s", session_directory)
    except Exception as exc:
        logger.exception("Pipeline crashed for %s", session_directory)
        _finish_manifest(session_directory, "error",
                         f"Processing failed: {exc}", warnings)


# --------------------------------------------------------------------------
# Page content loading (used by the Gemini worker when feeding pages)
# --------------------------------------------------------------------------

def load_page_content(session_directory, page):
    """Return the page's content: bytes for images, str for text pages."""
    path = os.path.join(session_directory, page["file"])
    if page["kind"] == "image":
        with open(path, "rb") as f:
            return f.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_initial_text(session_directory):
    """The optional free-text context the student supplied at upload."""
    path = os.path.join(session_directory, INITIAL_TEXT_NAME)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
