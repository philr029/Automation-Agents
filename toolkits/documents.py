"""
Reading the document formats that actually turn up in real work.

`toolkits/files.py` deliberately refuses anything that isn't plain text, so
a PDF never gets dumped into the context window as binary noise. This module
is the other half of that deal: it extracts *text* from PDFs, Word documents
and spreadsheets so the same agents can read them properly.

The parsing libraries are optional. If `pypdf` isn't installed, the PDF tool
returns an instruction to install it rather than crashing the run — an agent
that can read three of four formats is more useful than one that dies on
import.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree

from agentkit.safety import resolve_in_workspace
from agentkit.tools import tool
from core.config import Config


def _missing(library: str, purpose: str) -> str:
    """A readable install instruction, returned to the model as a tool result."""
    return (
        f"Cannot {purpose}: the {library!r} package is not installed. "
        f"Run `pip install {library}` and try again."
    )


@tool
def read_pdf(config: Config, path: str, max_chars: int = 20000, pages: str = "all") -> str:
    """Extract the text from a PDF file.

    path: PDF file, relative to the workspace root
    max_chars: truncate the extracted text after this many characters
    pages: 'all', a single page like '3', or a range like '1-5' (1-indexed)
    """
    target = resolve_in_workspace(config, path)
    if not target.is_file():
        return f"No such file: {path}"

    try:
        from pypdf import PdfReader
    except ImportError:
        return _missing("pypdf", "read PDFs")
    except Exception as exc:  # noqa: BLE001
        # A broken transitive dependency (pypdf pulls in `cryptography`,
        # which pulls in a compiled `cffi` backend) does not raise
        # ImportError — it can raise anything, including a panic from a
        # Rust extension. Catching only ImportError would take the whole
        # run down over one unreadable file format.
        return (
            f"The 'pypdf' package is installed but failed to load: "
            f"{type(exc).__name__}: {exc}. Try `pip install --force-reinstall cffi cryptography`."
        )

    try:
        reader = PdfReader(str(target))
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types
        return f"Could not open {path} as a PDF: {type(exc).__name__}: {exc}"

    if reader.is_encrypted:
        # An empty password unlocks a surprising number of "protected" PDFs.
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            return f"{path} is password-protected and cannot be read."

    total = len(reader.pages)
    try:
        selected = _parse_page_range(pages, total)
    except ValueError as exc:
        return str(exc)

    chunks: list[str] = []
    for index in selected:
        text = reader.pages[index].extract_text() or ""
        if text.strip():
            chunks.append(f"--- page {index + 1} ---\n{text.strip()}")

    if not chunks:
        return (
            f"{path} has {total} page(s) but no extractable text. It is most "
            "likely a scan; it would need OCR, which this toolkit does not do."
        )

    joined = "\n\n".join(chunks)
    if len(joined) > max_chars:
        return joined[:max_chars] + f"\n\n[truncated — {total} pages, {len(joined)} chars total]"
    return joined


def _parse_page_range(pages: str, total: int) -> list[int]:
    """Turn 'all' / '3' / '1-5' into a list of 0-indexed page numbers."""
    pages = (pages or "all").strip().lower()
    if pages in {"all", ""}:
        return list(range(total))

    if "-" in pages:
        start_text, _, end_text = pages.partition("-")
        try:
            start, end = int(start_text), int(end_text)
        except ValueError:
            raise ValueError(f"Could not read {pages!r} as a page range.") from None
    else:
        try:
            start = end = int(pages)
        except ValueError:
            raise ValueError(f"Could not read {pages!r} as a page number.") from None

    # Clamp to the document rather than erroring: asking for pages 1-100 of a
    # 10-page PDF should give you the 10 pages, not a complaint.
    start = max(1, start)
    end = min(total, end)
    if start > end:
        raise ValueError(f"Page range {pages!r} is empty for a {total}-page document.")
    return list(range(start - 1, end))


# Word files are zip archives of XML. This is the namespace its text lives in.
_DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@tool
def read_docx(config: Config, path: str, max_chars: int = 20000) -> str:
    """Extract the text from a Word (.docx) document.

    path: .docx file, relative to the workspace root
    max_chars: truncate the extracted text after this many characters
    """
    target = resolve_in_workspace(config, path)
    if not target.is_file():
        return f"No such file: {path}"

    # No dependency needed: a .docx is a zip containing word/document.xml,
    # and the paragraph text is plain enough to pull out with the stdlib.
    try:
        with zipfile.ZipFile(target) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        return f"{path} is not a readable .docx file (it may be an old .doc)."

    root = ElementTree.fromstring(xml)
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_DOCX_NS}p"):
        # A paragraph's text is split across any number of <w:t> runs;
        # joining them back together is the whole job.
        text = "".join(node.text or "" for node in paragraph.iter(f"{_DOCX_NS}t"))
        if text.strip():
            paragraphs.append(text.strip())

    if not paragraphs:
        return f"{path} contains no extractable text."

    joined = "\n\n".join(paragraphs)
    if len(joined) > max_chars:
        return joined[:max_chars] + f"\n\n[truncated — {len(joined)} chars total]"
    return joined


@tool
def read_document(config: Config, path: str, max_chars: int = 20000) -> str:
    """Read any supported document, picking the right reader by file extension.

    path: file to read, relative to the workspace root
    max_chars: truncate the extracted text after this many characters
    """
    # A single entry point the model can reach for without having to reason
    # about formats. Agents that process mixed folders should use this.
    suffix = Path(path).suffix.lower()

    if suffix == ".pdf":
        return read_pdf(config=config, path=path, max_chars=max_chars)
    if suffix == ".docx":
        return read_docx(config=config, path=path, max_chars=max_chars)

    from toolkits.files import read_file

    return read_file(config=config, path=path, max_chars=max_chars)


@tool
def list_documents(config: Config, subdirectory: str = ".") -> str:
    """List every readable document in a folder, grouped by format.

    subdirectory: folder to scan, relative to the workspace root
    """
    from toolkits.files import TEXT_SUFFIXES

    root = resolve_in_workspace(config, subdirectory)
    if not root.is_dir():
        return f"{subdirectory} is not a directory."

    readable = TEXT_SUFFIXES | {".pdf", ".docx"}
    groups: dict[str, list[str]] = {}
    skipped: list[str] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = str(path.relative_to(config.workspace))
        suffix = path.suffix.lower() or "(no extension)"
        if suffix in readable:
            groups.setdefault(suffix, []).append(relative)
        else:
            skipped.append(relative)

    if not groups:
        return f"No readable documents found in {subdirectory}."

    lines = []
    for suffix, paths in sorted(groups.items()):
        lines.append(f"{suffix} ({len(paths)}):")
        lines.extend(f"  {p}" for p in paths)

    if skipped:
        lines.append(f"\nUnreadable formats, skipped ({len(skipped)}): {', '.join(skipped[:10])}")

    return "\n".join(lines)
