"""Prepare readable PDF derivatives while preserving attachment originals."""

import io
import json
import secrets
import warnings
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from pypdf import PdfReader

from .desktop_inputs import reserve_id
from .ingest import InputError


def attachment_pdf(payload, suffix):
    if not payload or len(payload) > 32 * 1024 * 1024:
        raise InputError("Attachments must be nonempty and at most 32 MB.")
    if suffix == ".pdf":
        try:
            reader = PdfReader(io.BytesIO(payload))
            if reader.is_encrypted or not 1 <= len(reader.pages) <= 100:
                raise ValueError("Unsupported PDF")
        except Exception as exc:
            raise InputError("Choose an unencrypted PDF with 1–100 pages.") from exc
        return payload
    output = io.BytesIO()
    if suffix in {".jpg", ".jpeg", ".png"}:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as original:
                    if original.format not in {"PNG", "JPEG"} or getattr(original, "n_frames", 1) != 1:
                        raise ValueError("Unsupported image")
                    image = ImageOps.exif_transpose(original)
                    image.thumbnail((4000, 4000))
                    background = Image.new("RGB", image.size, "white")
                    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                        rgba = image.convert("RGBA")
                        background.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        background.paste(image.convert("RGB"))
                    background.save(output, "PDF", resolution=150)
        except Exception as exc:
            raise InputError("Choose a valid, single-image PNG or JPEG attachment.") from exc
    elif suffix == ".txt":
        try:
            text = payload.decode("utf-8").strip()
        except UnicodeError as exc:
            raise InputError("Text attachments must use UTF-8.") from exc
        if not text or len(text) > 20000:
            raise InputError("Enter 1–20,000 characters of attachment text.")
        # macOS supplies this Unicode font. The derivative embeds rendered pixels,
        # so recipients need neither this font nor macOS to view the resulting PDF.
        font_path = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
        if not font_path.exists():
            raise InputError("The system Unicode font is unavailable. Add a PDF or image instead.")
        font = ImageFont.truetype(str(font_path), 28)
        pages, lines, line = [], [], ""
        measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        for paragraph in text.expandtabs(4).split("\n"):
            for character in paragraph:
                if line and measure.textlength(line + character, font=font) > 1020:
                    lines.append(line)
                    line = ""
                line += character
            lines.append(line)
            line = ""
        if len(lines) > 4400:
            raise InputError("Text attachments must fit within 100 pages. Shorten the text or add a PDF.")
        for start in range(0, len(lines), 44):
            image = Image.new("RGB", (1240, 1754), "white")
            draw = ImageDraw.Draw(image)
            draw.text((100, 60), "Attachment text", font=font, fill="black")
            for index, value in enumerate(lines[start : start + 44]):
                draw.text((100, 130 + index * 35), value, font=font, fill="black")
            pages.append(image)
        pages[0].save(output, "PDF", resolution=150, save_all=True, append_images=pages[1:])
    else:
        raise InputError("Choose a PDF, PNG or JPEG, or enter attachment text.")
    return output.getvalue()


def stage_attachment(workspace, record_id, payload, filename, origin):
    if origin not in {"participant_material", "researcher_note"}:
        raise InputError("Specify whether this is participant material or a researcher note.")
    suffix = Path(filename).suffix.lower()
    derivative = attachment_pdf(payload, suffix)
    source_id = reserve_id(workspace, record_id)
    token = secrets.token_hex(20)
    directory = workspace.path / "desktop-imports" / token
    directory.mkdir(parents=True)
    (directory / "file-0.pdf").write_bytes(derivative)
    raw_name = "attachment-original" + suffix
    (directory / raw_name).write_bytes(payload)
    proposal = {
        "token": token,
        "entries": [{"file": "file-0.pdf", "filename": filename, "source_id": source_id}],
        "metadata": [],
        "attachment_notice": "Adding this source invalidates affected reviews. Existing AI drafts have not incorporated it. Review manually or explicitly process the updated record; adding an attachment does not call AI.",
    }
    (directory / "upload.json").write_text(
        json.dumps(
            {
                "files": ["file-0.pdf"],
                "original_names": {"file-0.pdf": filename},
                "attachment_originals": {
                    "file-0.pdf": {
                        "file": raw_name,
                        "filename": filename,
                        "origin": origin,
                        "added_at": datetime.now(timezone.utc).isoformat(),
                        "source_id": source_id,
                    }
                },
                "proposal": proposal,
            }
        )
    )
    return proposal


def previous_attachment_draft(session, record, snapshot):
    """Return a baseline only for unchanged scans plus additional attachments.

    This does not create a candidate: the owner explicitly chooses manual review
    or paid processing first. Other reviewers' answers are never copied.
    """
    from sqlalchemy import select

    from .db import Candidate, InputRevision
    from .schemas import Issue, PageCoverage, RecordContent

    current = snapshot["documents"]
    for candidate in session.scalars(
        select(Candidate)
        .where(
            Candidate.record_pk == record.id,
            Candidate.input_version < record.input_version,
        )
        .order_by(Candidate.created_at.desc(), Candidate.id.desc())
    ):
        revision = session.scalar(
            select(InputRevision).where(
                InputRevision.record_pk == record.id,
                InputRevision.version == candidate.input_version,
            )
        )
        if not revision:
            continue
        old = revision.snapshot["documents"]
        added = set(current) - set(old)
        if not added or set(old) - set(current):
            continue
        if any(current[sid]["sha256"] != doc["sha256"] for sid, doc in old.items()):
            continue
        if any(current[sid]["role"] != "attachment" for sid in added):
            continue
        content = RecordContent.model_validate(candidate.content).model_copy(deep=True)
        for page in content.pages:
            page.reviewed = False
        for sid in sorted(added):
            content.pages.extend(
                PageCoverage(
                    source_record_id=sid,
                    page_number=number,
                    classification="clarification",
                )
                for number in range(1, current[sid]["page_count"] + 1)
            )
            content.issues.append(
                Issue(
                    code="attachment_added_after_transcription",
                    severity="warning",
                    target=sid,
                    message="This attachment was added after the saved transcription. It has not been interpreted by AI. Check it and revise the food items as needed.",
                )
            )
        return content, {
            "method": "manual_attachment_update",
            "source_candidate_id": candidate.id,
            "transcription_input_version": candidate.input_version,
            "attachments_requiring_review": sorted(added),
        }
    return None
