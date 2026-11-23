"""Load, sanitise and chunk policy documents (Markdown, FAQ YAML, PDF)."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from guardrails.injection import detect_document_injection

log = logging.getLogger(__name__)

HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
HTML_TAG = re.compile(r"</?[a-zA-Z][^>]{0,200}>")
ZERO_WIDTH = re.compile(r"[​-‏⁠-⁤﻿­]")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
QUARANTINE_THRESHOLD = 0.6


@dataclass
class Section:
    title: str
    text: str
    page: int | None = None


@dataclass
class Document:
    doc_id: str
    title: str
    source: str
    kind: str
    sections: list[Section] = field(default_factory=list)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    title: str
    section: str
    text: str
    source: str
    kind: str
    page: int | None = None
    position: int = 0  # section index within the document (core rules usually come first)

    def payload(self) -> dict:
        return asdict(self)

    @property
    def embed_text(self) -> str:
        return f"{self.title}. {self.section}. {self.text}"


@dataclass
class SanitizeReport:
    removed_hidden_markup: int = 0
    quarantined: list[dict] = field(default_factory=list)


def sanitize(text: str, report: SanitizeReport | None = None) -> str:
    """Strip markup and invisible characters that can smuggle instructions into retrieved text."""
    before = text
    text = HTML_COMMENT.sub(" ", text)
    text = HTML_TAG.sub(" ", text)
    text = ZERO_WIDTH.sub("", CONTROL.sub(" ", text))
    if report is not None and text != before:
        report.removed_hidden_markup += 1
    return re.sub(r"[ \t]+", " ", text).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "section"


def load_markdown(path: Path) -> Document:
    raw = path.read_text(encoding="utf-8")
    title = path.stem.replace("-", " ").title()
    sections: list[Section] = []
    current_title, buffer = "Overview", []
    for line in raw.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            preamble = current_title == "Overview" and len("".join(buffer).strip()) < 200
            if "".join(buffer).strip() and not preamble:  # skip the one-line "version" preamble
                sections.append(Section(current_title, "\n".join(buffer).strip()))
            current_title, buffer = line[3:].strip(), []
        else:
            buffer.append(line)
    if "".join(buffer).strip():
        sections.append(Section(current_title, "\n".join(buffer).strip()))
    return Document(path.stem, title, path.name, "policy", sections)


def load_faq(path: Path) -> Document:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    sections = [Section(item["q"], f"Q: {item['q']}\nA: {item['a']}") for item in raw.get("faqs", [])]
    return Document(path.stem, raw.get("title", "FAQ"), path.name, "faq", sections)


def load_pdf(path: Path) -> Document:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    title = (reader.metadata.title if reader.metadata and reader.metadata.title else path.stem).strip()
    sections: list[Section] = []
    for page_no, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # Heuristic: short lines without a full stop are headings.
        current, buffer = f"Page {page_no}", []
        for line in lines:
            if len(line) < 40 and not line.endswith(".") and not line.endswith(","):
                if buffer:
                    sections.append(Section(current, " ".join(buffer), page_no))
                current, buffer = line, []
            else:
                buffer.append(line)
        if buffer:
            sections.append(Section(current, " ".join(buffer), page_no))
    return Document(path.stem, title, path.name, "pdf", sections)


def load_documents(directory: Path) -> list[Document]:
    docs: list[Document] = []
    for path in sorted(directory.glob("*")):
        if path.suffix == ".md":
            docs.append(load_markdown(path))
        elif path.suffix in (".yaml", ".yml"):
            docs.append(load_faq(path))
        elif path.suffix == ".pdf":
            docs.append(load_pdf(path))
    return docs


def _split(text: str, max_chars: int, overlap_sentences: int = 1) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks, current = [], []
    for sentence in sentences:
        if current and len(" ".join(current + [sentence])) > max_chars:
            chunks.append(" ".join(current))
            current = current[-overlap_sentences:] if overlap_sentences else []
        current.append(sentence)
    if current:
        chunks.append(" ".join(current))
    return chunks


def chunk_documents(
    docs: list[Document], max_chars: int = 600, report: SanitizeReport | None = None
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for doc in docs:
        for position, section in enumerate(doc.sections):
            section_id = f"{doc.doc_id}#{_slug(section.title)}"
            kept = []
            # Sentence-level quarantine: drop text that tries to instruct the model, keep the real policy around it.
            for sentence in re.split(r"(?<=[.!?])\s+", sanitize(section.text, report)):
                verdict = detect_document_injection(sentence)
                if verdict.score >= QUARANTINE_THRESHOLD:
                    log.warning("quarantined text in %s (signals=%s)", section_id, verdict.signals)
                    if report is not None:
                        report.quarantined.append({"chunk_id": section_id, "signals": verdict.signals, "text": sentence[:160]})
                    continue
                kept.append(sentence)
            clean = " ".join(kept).strip()
            if not clean:
                continue
            for n, piece in enumerate(_split(clean, max_chars)):
                if len(piece) < 90 and re.search(r"\bversion \d{4}-\d{2}\b", piece, re.I):
                    continue  # document metadata line, not policy content
                chunk_id = section_id + (f"-{n + 1}" if n else "")
                chunks.append(
                    Chunk(
                        chunk_id, doc.doc_id, doc.title, section.title, piece, doc.source, doc.kind, section.page, position
                    )
                )
    return chunks


def corpus_fingerprint(chunks: list[Chunk], embedder_name: str) -> str:
    digest = hashlib.sha256(embedder_name.encode())
    for chunk in chunks:
        digest.update(chunk.chunk_id.encode())
        digest.update(chunk.text.encode())
    return digest.hexdigest()[:16]
