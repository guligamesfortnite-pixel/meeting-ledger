"""
Builds a downloadable PDF or Word (.docx) version of a meeting - summary,
action items, and transcript. Uses reportlab (PDF) and python-docx
(Word), both pure-Python libraries with no system dependencies, so they
install cleanly on any host without extra setup.
"""
import io


def build_meeting_pdf(meeting, action_items):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.enums import TA_LEFT

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.75 * inch, bottomMargin=0.75 * inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("MeetingTitle", parent=styles["Title"], alignment=TA_LEFT, fontSize=20)
    meta_style = ParagraphStyle("Meta", parent=styles["Normal"], textColor="#666666", spaceAfter=16)
    heading_style = ParagraphStyle("SectionHeading", parent=styles["Heading2"], spaceBefore=14, spaceAfter=6)
    body_style = styles["Normal"]

    story = [Paragraph(_escape(meeting["title"]), title_style)]
    meta_text = meeting["meeting_date"]
    if meeting["attendees"]:
        meta_text += f"  ·  {_escape(meeting['attendees'])}"
    story.append(Paragraph(meta_text, meta_style))

    story.append(Paragraph("Summary", heading_style))
    story.append(Paragraph(_escape(meeting["summary"]) if meeting["summary"] else "No summary available.", body_style))

    story.append(Paragraph("Action Items", heading_style))
    if action_items:
        for item in action_items:
            mark = "\u2611" if item["done"] else "\u2610"
            owner = f" ({_escape(item['owner'])})" if item["owner"] else ""
            story.append(Paragraph(f"{mark} {_escape(item['description'])}{owner}", body_style))
    else:
        story.append(Paragraph("No action items.", body_style))

    if meeting["transcript"]:
        story.append(Paragraph("Full Transcript", heading_style))
        for line in meeting["transcript"].splitlines():
            story.append(Paragraph(_escape(line) if line.strip() else "&nbsp;", body_style))

    doc.build(story)
    buffer.seek(0)
    return buffer.read()


def build_meeting_docx(meeting, action_items):
    from docx import Document
    from docx.shared import Pt

    document = Document()
    document.add_heading(meeting["title"], level=1)

    meta = document.add_paragraph()
    meta_text = meeting["meeting_date"]
    if meeting["attendees"]:
        meta_text += f"  ·  {meeting['attendees']}"
    meta_run = meta.add_run(meta_text)
    meta_run.font.size = Pt(10)

    document.add_heading("Summary", level=2)
    document.add_paragraph(meeting["summary"] or "No summary available.")

    document.add_heading("Action Items", level=2)
    if action_items:
        for item in action_items:
            mark = "\u2611" if item["done"] else "\u2610"
            owner = f" ({item['owner']})" if item["owner"] else ""
            document.add_paragraph(f"{mark} {item['description']}{owner}")
    else:
        document.add_paragraph("No action items.")

    if meeting["transcript"]:
        document.add_heading("Full Transcript", level=2)
        document.add_paragraph(meeting["transcript"])

    buffer = io.BytesIO()
    document.save(buffer)
    buffer.seek(0)
    return buffer.read()


def _escape(text):
    """reportlab's Paragraph interprets a small set of HTML-like tags, so
    plain text needs its angle brackets and ampersands escaped first."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
