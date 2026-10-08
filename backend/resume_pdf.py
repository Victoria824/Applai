"""resume_pdf.py — 从结构化简历生成 PDF（改写版本用）。"""
import io

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


def _style(name, **kw):
    base = dict(fontName="Helvetica", fontSize=10, leading=14,
                spaceAfter=4, textColor="#1a1a1a")
    base.update(kw)
    return ParagraphStyle(name, **base)


STYLES = {
    "name": _style("name", fontSize=18, leading=22, fontName="Helvetica-Bold",
                   spaceAfter=2, alignment=1),
    "contact": _style("contact", fontSize=9, leading=12, textColor="#555",
                      alignment=1, spaceAfter=10),
    "h2": _style("h2", fontSize=12, leading=15, fontName="Helvetica-Bold",
                 spaceBefore=10, spaceAfter=4, textColor="#111",
                 borderPadding=(0, 0, 3)),
    "job": _style("job", fontSize=10, leading=13, fontName="Helvetica-Bold",
                  spaceAfter=1),
    "dates": _style("dates", fontSize=9, leading=12, textColor="#555",
                    spaceAfter=2),
    "bullet": _style("bullet", fontSize=10, leading=14, leftIndent=14,
                     bulletIndent=6, spaceAfter=2),
    "body": _style("body", fontSize=10, leading=14),
}


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generate_pdf(content: dict, profile: dict = None) -> bytes:
    """content: {summary, experiences[{company,title,dates,bullets}], skills[], education}.
    profile: 画像里的姓名/联系方式。返回 PDF bytes。"""
    profile = profile or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    story = []
    S = STYLES

    name = profile.get("fullName") or f"{profile.get('firstName','')} {profile.get('lastName','')}".strip()
    if name:
        story.append(Paragraph(_esc(name), S["name"]))
    contact = " | ".join(x for x in [
        profile.get("email", ""), profile.get("phone", ""), profile.get("location", ""),
        profile.get("linkedin", ""), profile.get("github", "")] if x)
    if contact:
        story.append(Paragraph(_esc(contact), S["contact"]))

    if content.get("summary"):
        story.append(Paragraph("Summary", S["h2"]))
        story.append(Paragraph(_esc(content["summary"]), S["body"]))

    exps = content.get("experiences") or []
    if exps:
        story.append(Paragraph("Experience", S["h2"]))
        for e in exps:
            title = f"{e.get('title','')} — {e.get('company','')}".strip(" —")
            if title:
                story.append(Paragraph(_esc(title), S["job"]))
            if e.get("dates"):
                story.append(Paragraph(_esc(e["dates"]), S["dates"]))
            for b in e.get("bullets") or []:
                story.append(Paragraph(_esc(b), S["bullet"], bulletText="•"))

    skills = content.get("skills") or []
    if skills:
        story.append(Paragraph("Skills", S["h2"]))
        story.append(Paragraph(_esc(", ".join(skills)), S["body"]))

    if content.get("education"):
        story.append(Paragraph("Education", S["h2"]))
        story.append(Paragraph(_esc(content["education"]), S["body"]))

    doc.build(story)
    return buf.getvalue()
