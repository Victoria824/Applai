"""resume_pdf.py — 从结构化简历生成 PDF（按岗定制版用）。

设计目标（Victoria 2026-10-20 要求）：
- 专业排版：深蓝主色、姓名大标题 + 强调线、章节全大写带细线、
  职位行左标题右日期对齐、紧凑而可读的字号/行距。
- 严格一页：先按比例缩放字号/行距/间距（1.0 → 0.70 共六档）适配一页；
  仍超页时，从最早的工作经历开始逐条删减 bullet（每段至少保留 1 条）。
- 内容只来自传入的 content（改写层已保证不编造），这里只负责呈现。
"""
import io

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (HRFlowable, Paragraph, SimpleDocTemplate,
                                Table, TableStyle)

NAVY = "#123c63"
NAVY_LIGHT = "#7d9dbd"
GRAY = "#555f6a"
INK = "#1c2733"

MARGIN_X = 0.62 * inch
MARGIN_TOP = 0.5 * inch
MARGIN_BOTTOM = 0.45 * inch


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _styles(scale: float) -> dict:
    def st(name, **kw):
        base = dict(fontName="Helvetica", fontSize=10, leading=13.5,
                    textColor=INK, spaceAfter=0)
        base.update(kw)
        base["fontSize"] = base["fontSize"] * scale
        base["leading"] = base["leading"] * scale
        if "spaceBefore" in base:
            base["spaceBefore"] = base["spaceBefore"] * scale
        if "spaceAfter" in base:
            base["spaceAfter"] = base["spaceAfter"] * scale
        return ParagraphStyle(name, **base)

    return {
        "name": st("name", fontName="Helvetica-Bold", fontSize=20, leading=24,
                   textColor=NAVY, spaceAfter=2),
        "contact": st("contact", fontSize=8.6, leading=11, textColor=GRAY,
                      spaceAfter=2),
        "h2": st("h2", fontName="Helvetica-Bold", fontSize=10.5, leading=13,
                 textColor=NAVY, spaceBefore=9),
        "job": st("job", fontName="Helvetica-Bold", fontSize=10.3, leading=13),
        "dates": st("dates", fontSize=8.8, leading=12, textColor=GRAY,
                    alignment=2),
        "bullet": st("bullet", fontSize=9.8, leading=12.8, leftIndent=11,
                     bulletIndent=3, spaceAfter=1.5),
        "body": st("body", fontSize=9.8, leading=13),
        "edu": st("edu", fontSize=9.8, leading=12.8, spaceAfter=1.5),
    }


def _build_story(content: dict, profile: dict, scale: float) -> list:
    S = _styles(scale)
    story = []

    name = profile.get("fullName") or \
        f"{profile.get('firstName','')} {profile.get('lastName','')}".strip()
    if name:
        story.append(Paragraph(_esc(name), S["name"]))
    contact = "  ·  ".join(x for x in [
        profile.get("email", ""), profile.get("phone", ""),
        profile.get("location", ""), profile.get("linkedin", ""),
        profile.get("github", "")] if x)
    if contact:
        story.append(Paragraph(_esc(contact), S["contact"]))
    story.append(HRFlowable(width="100%", thickness=1.4, color=NAVY,
                            spaceBefore=3 * scale, spaceAfter=2 * scale))

    def section(title):
        story.append(Paragraph(title.upper(), S["h2"]))
        story.append(HRFlowable(width="100%", thickness=0.6, color=NAVY_LIGHT,
                                spaceBefore=0, spaceAfter=4 * scale))

    if content.get("summary"):
        section("Summary")
        story.append(Paragraph(_esc(content["summary"]), S["body"]))

    exps = content.get("experiences") or []
    if exps:
        section("Experience")
        for e in exps:
            title = f"{e.get('title','')} — {e.get('company','')}".strip(" —")
            head = Table(
                [[Paragraph(_esc(title), S["job"]),
                  Paragraph(_esc(e.get("dates", "")), S["dates"])]],
                colWidths=[5.4 * inch, 1.86 * inch])
            head.setStyle(TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * scale),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(head)
            for b in e.get("bullets") or []:
                story.append(Paragraph(_esc(b), S["bullet"], bulletText="•"))

    skills = content.get("skills") or []
    if skills:
        section("Skills")
        story.append(Paragraph(_esc(", ".join(skills)), S["body"]))

    edu = content.get("education")
    if edu:
        section("Education")
        lines = edu if isinstance(edu, list) else [edu]
        for ln in lines:
            if isinstance(ln, dict):
                ln = " — ".join(str(ln.get(k, "")) for k in
                                ("school", "degree", "dates") if ln.get(k))
            story.append(Paragraph(_esc(str(ln)), S["edu"]))
    return story


def _doc_template(buf) -> SimpleDocTemplate:
    return SimpleDocTemplate(buf, pagesize=letter,
                             leftMargin=MARGIN_X, rightMargin=MARGIN_X,
                             topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM)


def _fits(story, doc) -> bool:
    total = 0.0
    for fl in story:
        try:
            _, h = fl.wrap(doc.width, 100000)
        except Exception:
            return False
        total += h + fl.getSpaceBefore() + fl.getSpaceAfter()
        if total > doc.height:
            return False
    return True


def _trim_bullets(content: dict) -> bool:
    """从最晚（列表末尾，通常是最早）的一段经历开始删一条 bullet
    （保留每段至少 1 条）。删了返回 True。"""
    exps = content.get("experiences") or []
    for e in reversed(exps):
        bullets = e.get("bullets") or []
        if len(bullets) > 1:
            bullets.pop()
            e["bullets"] = bullets
            return True
    return False


def generate_pdf(content: dict, profile: dict = None) -> bytes:
    """content: {summary, experiences[{company,title,dates,bullets}], skills[], education}。
    profile: 画像里的姓名/联系方式。返回恰好一页的 PDF bytes（内容极端多时
    会先缩放排版、再删减最早经历的 bullet 来保证一页）。"""
    import copy
    profile = profile or {}
    content = copy.deepcopy(content or {})

    for scale in (1.0, 0.94, 0.88, 0.82, 0.76, 0.70):
        story = _build_story(content, profile, scale)
        probe = _doc_template(io.BytesIO())
        if _fits(story, probe):
            buf = io.BytesIO()
            _doc_template(buf).build(story)
            return buf.getvalue()

    # 最小字号仍超页：逐条删减最早经历的 bullet 直到装下
    for _ in range(60):
        if not _trim_bullets(content):
            break
        story = _build_story(content, profile, 0.70)
        probe = _doc_template(io.BytesIO())
        if _fits(story, probe):
            buf = io.BytesIO()
            _doc_template(buf).build(story)
            return buf.getvalue()

    buf = io.BytesIO()
    _doc_template(buf).build(_build_story(content, profile, 0.70))
    return buf.getvalue()
