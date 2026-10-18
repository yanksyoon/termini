# -*- coding: utf-8 -*-
"""Build deck v3 on the AI Builder Cup submission template."""
import copy
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.oxml.ns import qn
from lxml import etree

TPL = "/home/ubuntu/Submission Template _ AI Builder Cup.pptx"
OUT = "/home/ubuntu/.worktrees/deck-v3/deck/v3/termini-deck-v3.pptx"
HEADER = "/home/ubuntu/.hermes/cache/scratch/tpl/s3_0_png_8554.png"
HAIRLINE = "/home/ubuntu/.hermes/cache/scratch/tpl/s1_2_png_209476.png"
SHOT = "/home/ubuntu/.worktrees/deck-v3/deck/v3/snapshot-incidents.png"
CLOSING = "/home/ubuntu/.hermes/cache/scratch/tpl/s16_1_png_186669.png"

INK   = RGBColor(0x20, 0x21, 0x24)
MUTED = RGBColor(0x5F, 0x63, 0x68)
FAINT = RGBColor(0x9A, 0xA0, 0xA6)
LINE  = RGBColor(0xDA, 0xDC, 0xE0)
PANEL = RGBColor(0xF1, 0xF3, 0xF4)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
BLUE  = RGBColor(0x1A, 0x73, 0xE8)
GREEN = RGBColor(0x18, 0x80, 0x38)
RED   = RGBColor(0xD9, 0x30, 0x25)
AMBER = RGBColor(0xB0, 0x60, 0x00)

BODY = "Arial"
TITLEFONT = "Google Sans Flex Medium"

prs = Presentation(TPL)
BLANK = None
for L in prs.slide_layouts:
    if L.name == "BLANK":
        BLANK = L

# ---------------------------------------------------------------- helpers
def _tb(slide, x, y, w, h):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = 0; tf.margin_right = 0
    tf.margin_top = 0; tf.margin_bottom = 0
    return box

def txt(slide, x, y, w, h, text, size=11, bold=False, color=INK,
        font=BODY, align=PP_ALIGN.LEFT, spacing=1.12, anchor=MSO_ANCHOR.TOP):
    box = _tb(slide, x, y, w, h)
    tf = box.text_frame
    tf.vertical_anchor = anchor
    lines = text.split("\n")
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        r = p.add_run(); r.text = line
        r.font.size = Pt(size); r.font.bold = bold
        r.font.color.rgb = color; r.font.name = font
    return box

def rich(slide, x, y, w, h, parts, size=11, spacing=1.14, align=PP_ALIGN.LEFT):
    """parts: list of paragraphs, each a list of (text, dict(bold=,color=,size=))"""
    box = _tb(slide, x, y, w, h)
    tf = box.text_frame
    for i, para in enumerate(parts):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        if i:
            p.space_before = Pt(5)
        for text, st in para:
            r = p.add_run(); r.text = text
            r.font.size = Pt(st.get("size", size))
            r.font.bold = st.get("bold", False)
            r.font.italic = st.get("italic", False)
            r.font.color.rgb = st.get("color", INK)
            r.font.name = BODY
    return box

def rect(slide, x, y, w, h, fill=PANEL, line=None, radius=None, shape=MSO_SHAPE.ROUNDED_RECTANGLE):
    sh = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if fill is None:
        sh.fill.background()
    else:
        sh.fill.solid(); sh.fill.fore_color.rgb = fill
    if line is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line; sh.line.width = Pt(0.75)
    if radius is not None and shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        try:
            sh.adjustments[0] = radius
        except Exception:
            pass
    sh.shadow.inherit = False
    _st = sh._element.find(qn('p:style'))
    if _st is not None:
        sh._element.remove(_st)
    tf = sh.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_left = Inches(0.08); tf.margin_right = Inches(0.08)
    tf.margin_top = Inches(0.04); tf.margin_bottom = Inches(0.04)
    return sh

def shape_text(sh, text, size=10, bold=False, color=INK, align=PP_ALIGN.LEFT, spacing=1.1):
    if align == PP_ALIGN.CENTER:
        sh.name = "centered-box"
    tf = sh.text_frame
    lines = text.split("\n")
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        r = p.add_run(); r.text = line
        r.font.size = Pt(size); r.font.bold = bold
        r.font.color.rgb = color; r.font.name = BODY

def link_runs(box, url):
    for para in box.text_frame.paragraphs:
        for run in para.runs:
            run.hyperlink.address = url
            run.font.color.rgb = BLUE

def new_slide(section, num):
    s = prs.slides.add_slide(BLANK)
    s.shapes.add_picture(HEADER, 0, 0, width=Inches(10), height=Inches(0.5549))
    bar = rect(s, 0, 5.47, 10.0, 0.17, fill=RGBColor(0, 0, 0), shape=MSO_SHAPE.RECTANGLE)
    txt(s, 0.29, 0.78, 7.6, 0.36, section, size=15, font=TITLEFONT, color=INK)
    txt(s, 9.1, 0.80, 0.61, 0.3, str(num), size=9, color=FAINT, align=PP_ALIGN.RIGHT)
    return s

def head(s, text, y=1.14, size=23):
    txt(s, 0.29, y, 9.42, 0.46, text, size=size, bold=True, color=INK)

def sub(s, text, y=1.6, size=10.5):
    txt(s, 0.29, y, 9.42, 0.34, text, size=size, color=MUTED)

def arrow(s, x, y, w=0.22, h=0.16, color=FAINT):
    a = s.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, Inches(x), Inches(y), Inches(w), Inches(h))
    a.fill.solid(); a.fill.fore_color.rgb = color
    a.line.fill.background(); a.shadow.inherit = False
    return a

def rule(s, x, y, w, color=LINE):
    ln = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(0.012))
    ln.fill.solid(); ln.fill.fore_color.rgb = color
    ln.line.fill.background(); ln.shadow.inherit = False
    return ln


def light_table(tbl):
    """Strip the default table borders; leave a hairline under each row."""
    for r in tbl.rows:
        for cell in r.cells:
            tcPr = cell._tc.get_or_add_tcPr()
            for tag in ('a:lnL', 'a:lnR', 'a:lnT', 'a:lnB'):
                for e in tcPr.findall(qn(tag)):
                    tcPr.remove(e)
            made = []
            for tag in ('a:lnL', 'a:lnR', 'a:lnT', 'a:lnB'):
                ln = etree.Element(qn(tag))
                if tag == 'a:lnB':
                    ln.set('w', '9525')
                    sf = etree.SubElement(ln, qn('a:solidFill'))
                    c = etree.SubElement(sf, qn('a:srgbClr')); c.set('val', 'DADCE0')
                else:
                    ln.set('w', '0')
                    etree.SubElement(ln, qn('a:noFill'))
                made.append(ln)
            for ln in reversed(made):
                tcPr.insert(0, ln)

def normalise(slide):
    for sh in slide.shapes:
        if not sh.has_text_frame or sh.name == "centered-box":
            continue
        for para in sh.text_frame.paragraphs:
            para.alignment = PP_ALIGN.LEFT

# ---------------------------------------------------------------- trim template: keep 1 and last
sldIdLst = prs.slides._sldIdLst
ids = list(sldIdLst)
for el in ids[1:]:
    prs.part.drop_rel(el.rId)   # drop the relationship too, or the orphan parts
    sldIdLst.remove(el)         # collide with the new slides' partnames on save

# ---------------------------------------------------------------- slide 1: team details
s1 = prs.slides[0]
for sh in s1.shapes:
    if sh.has_text_frame and "Team Details" in sh.text_frame.text:
        tf = sh.text_frame
        tf.clear()
        p = tf.paragraphs[0]
        r = p.add_run(); r.text = "Team Details"
        r.font.size = Pt(20); r.font.bold = True; r.font.name = TITLEFONT; r.font.color.rgb = INK
        rows = [
            ("Team name: ", "Termini"),
            ("Team leader name: ", "Florentiana Yuwono"),
            ("Problem Statement: ", "Future of Work & Enterprise Productivity. Every organisation should be able to run AI on data it cannot send to a cloud."),
        ]
        for label, value in rows:
            p = tf.add_paragraph(); p.space_before = Pt(6)
            r = p.add_run(); r.text = label
            r.font.size = Pt(12); r.font.bold = True; r.font.name = BODY; r.font.color.rgb = INK
            r = p.add_run(); r.text = value
            r.font.size = Pt(12); r.font.name = BODY; r.font.color.rgb = MUTED

# ---------------------------------------------------------------- 2 · brief 1
s = new_slide("Brief about the idea", 2)
head(s, "One clinic, one rule")
rich(s, 0.29, 1.62, 5.55, 1.7, [
    [("A dental clinic in Korea wanted AI for its phones, its insurance claims and its own reporting. "
      "Sending patient data to a cloud was not an option, so the owner built the infrastructure himself. "
      "He studied biology, not computer science.", {})]], size=11, spacing=1.2)
q = rect(s, 0.29, 2.75, 5.55, 0.9, fill=RGBColor(0xE8, 0xF0, 0xFE), line=None, radius=0.08)
shape_text(q, "“All client data must not leave the building.”", size=15, bold=True, color=BLUE)
rich(s, 0.29, 3.75, 5.55, 0.3, [[("The rule the whole build was organised around", {"size": 9, "color": MUTED})]])
rich(s, 0.29, 4.3, 5.55, 0.6, [[("Phones, insurance claims, reporting and document retrieval all run on models in the same room as the chair.", {"size": 9.5, "color": MUTED, "italic": True})]])
# right column facts
for i, (big, cap) in enumerate([("6 months", "of evenings, on top of dentistry. That is what it costs when no product exists."),
                                ("0", "client records sent off-site; phones, claims and reporting all run in the back room")]):
    y = 1.62 + i * 1.6
    txt(s, 6.35, y, 3.36, 0.5, big, size=30, bold=True, color=(INK if i == 0 else GREEN))
    txt(s, 6.35, y + 0.52, 3.36, 0.6, cap, size=9.5, color=MUTED, spacing=1.15)
rule(s, 0.29, 5.02, 9.42)
rich(s, 0.29, 5.1, 9.42, 0.3, [[("Source: ", {"size": 8, "color": FAINT}),
                                ("arca.live/b/aihomelab/185810092", {"size": 8, "color": BLUE}),
                                (". “A neighbourhood dental clinic's AX transformation”, 13 Oct 2026", {"size": 8, "color": FAINT})]])

# ---------------------------------------------------------------- 3 · brief 2
s = new_slide("Brief about the idea", 3)
head(s, "What can't leave the building")
sub(s, "The clinic is not unusual. Every business whose best data is also its most restricted lives by something like it.")
inds = [
    ("Healthcare & dental", "Patient records, calls, insurance claims", "Front desk, adjudication, notes"),
    ("Banking & insurance", "KYC files, transactions, loss records", "Fraud triage, credit memos, claims"),
    ("Legal", "Privileged matter files, client advice", "Contract review, discovery, drafting"),
    ("Manufacturing", "Design files, process parameters, yields", "Quality, maintenance, planning"),
    ("Life sciences", "Trial data, patient histories", "Protocol analysis, safety review"),
    ("Public sector & defence", "Classified material, citizen records", "Analysis, briefings, casework"),
    ("Software", "Source code, incident data, architecture", "Code agents, on-call triage"),
    ("Retail & commerce", "Customer PII, supplier terms, margins", "Demand, service, pricing"),
]
for i, (name, data, use) in enumerate(inds):
    cx = 0.29 + (i % 4) * 2.37
    cy = 1.95 + (i // 4) * 1.18
    c = rect(s, cx, cy, 2.24, 1.05, fill=PANEL, line=None, radius=0.06)
    tf = c.text_frame
    p = tf.paragraphs[0]; r = p.add_run(); r.text = name
    r.font.size = Pt(9.5); r.font.bold = True; r.font.color.rgb = INK; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(2); r = p.add_run(); r.text = data
    r.font.size = Pt(7.8); r.font.color.rgb = MUTED; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(2); r = p.add_run(); r.text = use
    r.font.size = Pt(7.8); r.font.color.rgb = BLUE; r.font.name = BODY
rich(s, 0.29, 4.36, 9.42, 0.32, [[("Eight industries, one constraint: the data that would make AI useful is the data that can't be handed over.", {"bold": True})]], size=11)
rich(s, 0.29, 4.78, 9.42, 0.5, [
    [("And the result: ", {"color": MUTED}),
     ("95%", {"bold": True, "color": RED}),
     (" of GenAI pilots show no measurable P&L impact (MIT NANDA 2025, contested) while ", {"color": MUTED}),
     ("88%", {"bold": True}),
     (" of firms use AI somewhere (McKinsey 2025). Permission, operations and context are missing at once.", {"color": MUTED})]], size=9.5)
rule(s, 0.29, 5.16, 9.42)
txt(s, 0.29, 5.23, 9.42, 0.25, "Sources: MIT NANDA, The GenAI Divide (Jul 2025) · McKinsey, The State of AI 2025", size=7.5, color=FAINT)

# ---------------------------------------------------------------- 4 · brief 3
s = new_slide("Brief about the idea", 4)
head(s, "Data used without permission")
sub(s, "The loud concern is AI replacing existing labour. The governing one arrives first: the work leaves, and comes back as someone else's product.")
cases = [
    ("Apr 2023 · Samsung", "Engineers debugged with ChatGPT",
     "Confidential source code, equipment yield data and a meeting's notes went in as prompts. Samsung restricted generative AI across its network weeks later.",
     "“Data is transmitted and stored to an external server, making it impossible for the company to retrieve it.”. Samsung internal memo",
     "fortune.com · 2 May 2023"),
    ("Jan 2025 · DeepSeek", "A database of everyone's prompts",
     "Wiz Research found an unauthenticated ClickHouse instance holding 1M+ log lines, chat history, API keys and plaintext passwords.",
     "The model was never breached. A copy of every conversation sat open on the internet.",
     "wiz.io/blog · 29 Jan 2025"),
    ("Sep 2026 · Navier–Stokes", "The credit left with the data",
     "OpenAI announced a Navier–Stokes solution its team described as using “very little human input”. Twelve hours earlier, Buckmaster said his and Alpöge's unpublished advances had leaked to the lab.",
     "A Millennium Prize result, and a priority dispute about whose work the model had read.",
     "wikipedia.org · Sept 2026"),
]
for i, (when, title, body_txt, quote, src) in enumerate(cases):
    cx = 0.29 + i * 3.18
    card = rect(s, cx, 1.88, 3.02, 2.28, fill=PANEL, line=None, radius=0.05)
    tf = card.text_frame
    tf.margin_left = Inches(0.12); tf.margin_right = Inches(0.12); tf.margin_top = Inches(0.1)
    p = tf.paragraphs[0]; r = p.add_run(); r.text = when
    r.font.size = Pt(8); r.font.bold = True; r.font.color.rgb = AMBER; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(3); r = p.add_run(); r.text = title
    r.font.size = Pt(11); r.font.bold = True; r.font.color.rgb = INK; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(4); r = p.add_run(); r.text = body_txt
    r.font.size = Pt(8.2); r.font.color.rgb = MUTED; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(6); r = p.add_run(); r.text = quote
    r.font.size = Pt(7.6); r.font.italic = True; r.font.color.rgb = RGBColor(0x3C, 0x40, 0x43); r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(5); r = p.add_run(); r.text = src
    r.font.size = Pt(7); r.font.color.rgb = BLUE; r.font.name = BODY
rich(s, 0.29, 4.42, 9.42, 0.5, [[("How Termini closes it: ", {"bold": True, "color": BLUE}),
                                  ("a topology where the copy never leaves. C2 stays on the node, and what does leave is named, classified and audited.", {})]], size=9.5)

# ---------------------------------------------------------------- 5 · brief 4
s = new_slide("Brief about the idea", 5)
head(s, "The loop only closes where the data lives")
sub(s, "A system improves by learning from its own outcomes. When the record lives in someone else's cloud, the loop belongs to someone else.")
nodes = ["Data owners", "Model builders", "Compute", "Applications", "Value"]
for i, n in enumerate(nodes):
    cx = 0.29 + i * 1.94
    b = rect(s, cx, 2.0, 1.72, 0.5, fill=WHITE, line=LINE, radius=0.12)
    shape_text(b, n, size=9.5, bold=True, align=PP_ALIGN.CENTER)
    b.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    if i < 4:
        arrow(s, cx + 1.73, 2.02, 0.19, 0.18)

def seg(x1, y1, x2, y2, dashed=False):
    c = s.shapes.add_connector(1, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = RGBColor(0xBD, 0xC1, 0xC6)
    c.line.width = Pt(1)
    _st = c._element.find(qn('p:style'))
    if _st is not None:
        c._element.remove(_st)
    if dashed:
        c.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    return c

seg(9.14, 2.5, 9.14, 2.74)
seg(9.14, 2.74, 6.95, 2.74, dashed=True)
seg(1.15, 2.5, 1.15, 2.74)
seg(3.5, 2.74, 1.15, 2.74, dashed=True)
chip = rect(s, 3.5, 2.55, 3.45, 0.38, fill=RGBColor(0xE8, 0xF0, 0xFE), line=BLUE, radius=0.4)
shape_text(chip, "sovereign AI: the missing arc", size=9.5, bold=True, color=BLUE, align=PP_ALIGN.CENTER)
chip.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
props = [("A memory that stays", "Outcomes, corrections and exceptions remain with the entity."),
         ("A jury, not an oracle", "Several models read the same evidence; disagreement is where a person looks."),
         ("A corpus no one else has", "The private record no model has seen and no vendor could be given.")]
for i, (t, d) in enumerate(props):
    cx = 0.29 + i * 3.18
    c = rect(s, cx, 3.24, 3.02, 1.1, fill=PANEL, line=None, radius=0.06)
    tf = c.text_frame
    p = tf.paragraphs[0]; r = p.add_run(); r.text = t
    r.font.size = Pt(10); r.font.bold = True; r.font.color.rgb = INK; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(3); r = p.add_run(); r.text = d
    r.font.size = Pt(8.2); r.font.color.rgb = MUTED; r.font.name = BODY
rich(s, 0.29, 4.62, 9.42, 0.5, [[("Intelligence should be plural. ", {"bold": True}),
                                  ("Governance is what makes the loop close.", {"color": BLUE, "bold": True})]], size=12)

# ---------------------------------------------------------------- 6 · opportunities 1
s = new_slide("Opportunities", 6)
head(s, "Right-size the intelligence")
sub(s, "Most tasks do not need a frontier model. The product is a ladder, and the routing between its levels.")
rows = [("Level", "Runs on", "Good at", "Data movement", "In the demo"),
        ("L0 · deterministic", "Controller rules", "Known remedies, threshold-to-action mappings", "None", "Yes"),
        ("L1 · small local", "The node: 4B class, llama.cpp", "Classify, extract, summarise, triage", "None", "Yes"),
        ("L2 · large local", "A GPU node over your data", "Reasoning specific to your domain data", "None", "Config only"),
        ("L3 · frontier", "Gemini, via Vertex AI", "The hardest reasoning, the smallest payload", "Minimised C0, if permitted", "Yes")]
tbl_shape = s.shapes.add_table(len(rows), 5, Inches(0.29), Inches(1.92), Inches(9.42), Inches(2.3))
tbl = tbl_shape.table
light_table(tbl)
for w, i in zip([1.55, 2.05, 3.05, 1.65, 1.12], range(5)):
    tbl.columns[i].width = Inches(w)
for ri, row in enumerate(rows):
    for ci, val in enumerate(row):
        cell = tbl.cell(ri, ci)
        cell.margin_left = Inches(0.06); cell.margin_right = Inches(0.05)
        cell.margin_top = Inches(0.03); cell.margin_bottom = Inches(0.03)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        cell.fill.solid()
        cell.fill.fore_color.rgb = WHITE if ri == 0 else (PANEL if ri % 2 else WHITE)
        tf = cell.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        r = p.add_run(); r.text = val
        r.font.size = Pt(8.5 if ri else 8)
        r.font.bold = (ci == 0) or (ri == 0)
        r.font.name = BODY
        if ri == 0:
            r.font.color.rgb = MUTED
        elif ci == 4:
            r.font.color.rgb = GREEN if val != "Config only" else AMBER
        elif ci == 0:
            r.font.color.rgb = BLUE
        else:
            r.font.color.rgb = INK if ci == 2 else MUTED
rich(s, 0.29, 4.42, 9.42, 0.5, [[("L1 triages → L3 reasons, only where policy allows → L0 executes → ", {"color": MUTED}),
                                  ("the audit records which level answered.", {"bold": True})]], size=10)
rich(s, 0.29, 4.82, 9.42, 0.4, [[("Local is the right level for most of the work.", {"bold": True, "color": BLUE})]], size=11)

# ---------------------------------------------------------------- 7 · opportunities 2
s = new_slide("Opportunities", 7)
head(s, "Delegate the operations, not the data")
sub(s, "That split is the whole proposition. Packaging, partners and pricing all follow from it.")
cards = [("What is delegated", "Bootstrap, remediation, upgrades, the on-call rotation. This is the work a small firm cannot staff and a hardware owner does not want to build.", BLUE),
         ("What is never delegated", "C2 payloads, transcripts, records, credentials. The data stays on the node; the controller sees only what the classification allows.", GREEN),
         ("Who buys which half", "The SME buys the outcome. The hardware owner licenses the layer so its metal is sellable as AI. Both get the same control plane.", AMBER)]
for i, (t, d, col) in enumerate(cards):
    cx = 0.29 + i * 3.18
    c = rect(s, cx, 2.25, 3.02, 1.55, fill=PANEL, line=None, radius=0.06)
    bar = rect(s, cx, 2.25, 0.05, 1.55, fill=col, shape=MSO_SHAPE.RECTANGLE)
    tf = c.text_frame
    tf.margin_left = Inches(0.16)
    p = tf.paragraphs[0]; r = p.add_run(); r.text = t
    r.font.size = Pt(11); r.font.bold = True; r.font.color.rgb = INK; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(5); r = p.add_run(); r.text = d
    r.font.size = Pt(8.8); r.font.color.rgb = MUTED; r.font.name = BODY
rich(s, 0.29, 4.4, 9.42, 0.6, [[("One control plane, two buyers, one boundary, ", {"color": MUTED}),
                                 ("and the boundary is what makes delegation signable.", {"bold": True})]], size=12)

# ---------------------------------------------------------------- 8 · features
s = new_slide("List of features offered by the solution", 8)
head(s, "Bootstrap. Operate. Prove.")
sub(s, "The clinic needed all three, so one man built all three by hand. In Termini they are one product.")
feats = [("01 · Bootstrap", "Hardware becomes a fleet",
          "A bare node boots the pre-baked image, registers in 150 ms, is assigned a role and joins the cluster. No installs, no specialist on site.", "Removes: the wiring weekend"),
         ("02 · Operate", "An operator you don't hire",
          "An AI agent watches telemetry and makes the calls a data-centre operator would: restarting a runaway workload, stopping what will fill a disk. When it is unsure it falls back to a fixed rule.", "Removes: the on-call rotation"),
         ("03 · Prove", "A boundary you can audit",
          "C0 / C1 / C2 classification, an on-node redactor and a fail-closed controller gate. What leaves is named, classified and logged.", "Removes: the compliance gap")]
for i, (k, t, d, rm) in enumerate(feats):
    cx = 0.29 + i * 3.18
    c = rect(s, cx, 2.2, 3.02, 2.08, fill=PANEL, line=None, radius=0.05)
    tf = c.text_frame
    tf.margin_left = Inches(0.12); tf.margin_top = Inches(0.1)
    p = tf.paragraphs[0]; r = p.add_run(); r.text = k
    r.font.size = Pt(8); r.font.bold = True; r.font.color.rgb = BLUE; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(3); r = p.add_run(); r.text = t
    r.font.size = Pt(12); r.font.bold = True; r.font.color.rgb = INK; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(6); r = p.add_run(); r.text = d
    r.font.size = Pt(8.5); r.font.color.rgb = MUTED; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(8); r = p.add_run(); r.text = rm
    r.font.size = Pt(8); r.font.italic = True; r.font.color.rgb = AMBER; r.font.name = BODY
rich(s, 0.29, 4.62, 9.42, 0.4, [[("The clinic is the requirement. ", {"color": MUTED}), ("Termini is the product.", {"bold": True})]], size=11)

# ---------------------------------------------------------------- 9 · process flow
s = new_slide("Process flow diagram or Use-case diagram", 9)
head(s, "From bare node to a governed fleet")
txt(s, 0.29, 1.62, 3.0, 0.3, "DAY-0 · BRING-UP", size=8.5, bold=True, color=BLUE)
d0 = ["Bare node", "Pre-baked image", "Registered 150 ms", "Role assigned", "Agents running"]
for i, n in enumerate(d0):
    cx = 0.29 + i * 1.94
    b = rect(s, cx, 2.0, 1.72, 0.5, fill=WHITE, line=LINE, radius=0.12)
    shape_text(b, n, size=9, bold=(i == 2), color=(BLUE if i == 2 else INK), align=PP_ALIGN.CENTER)
    b.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    if i < 4:
        arrow(s, cx + 1.73, 2.1, 0.19, 0.18)
txt(s, 0.29, 2.72, 4.0, 0.3, "DAY-2 · SELF-HEALING LOOP", size=8.5, bold=True, color=GREEN)
d2 = ["Telemetry", "Threshold rule fires", "Model decides", "Audited execution", "Audit row"]
for i, n in enumerate(d2):
    cx = 0.29 + i * 1.94
    b = rect(s, cx, 3.05, 1.72, 0.5, fill=WHITE, line=LINE, radius=0.12)
    shape_text(b, n, size=9, bold=(i == 4), color=(GREEN if i == 4 else INK), align=PP_ALIGN.CENTER)
    b.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    if i < 4:
        arrow(s, cx + 1.73, 3.2, 0.19, 0.18)
rich(s, 0.29, 3.85, 9.42, 0.7, [[("The rule detects; the model decides; the executor acts; the audit row links ", {"color": MUTED}),
                                 ("trigger → decision → action → result.", {"bold": True})]], size=10)
rich(s, 0.29, 4.2, 9.42, 0.4, [[("Nothing is decided by a model that a person could not read back afterwards. The deterministic path needs no model at all.", {"color": MUTED})]], size=9.5)
rich(s, 0.29, 4.62, 9.42, 0.4, [[("The action space is grammar-constrained: ", {"bold": True}),
                                  ("an off-allowlist action has no valid encoding, so safety is enforced in the decoder rather than checked afterwards.", {"color": MUTED})]], size=9.5)

# ---------------------------------------------------------------- 10 · architecture
s = new_slide("Architecture diagram of the proposed solution", 10)
head(s, "The architecture is the governance claim")
steps = [("Node on premises", "C2 stays here"), ("On-node redactor", "fail-closed"), ("Outbound-only mTLS", "no inbound path"),
         ("Controller gate", "re-verifies"), ("Operator loop", "allowlisted"), ("Audit chain", "evidence")]
for i, (n, d) in enumerate(steps):
    cx = 0.29 + i * 1.6
    b = rect(s, cx, 2.0, 1.4, 0.78, fill=WHITE, line=LINE, radius=0.1)
    tf = b.text_frame
    p = tf.paragraphs[0]; r = p.add_run(); r.text = n
    r.font.size = Pt(8.5); r.font.bold = True; r.font.color.rgb = INK; r.font.name = BODY
    p = tf.add_paragraph(); r = p.add_run(); r.text = d
    r.font.size = Pt(7.2); r.font.color.rgb = MUTED; r.font.name = BODY
    if i < 5:
        arrow(s, cx + 1.41, 2.3, 0.17, 0.16)
for i, (t, d) in enumerate([("Substrate and channel: adopted, not invented",
                             "Ubuntu and an LXD cluster across nodes; one outbound-only mTLS WebSocket per node carries heartbeat, telemetry, commands and acks. No route from an agent VM to the controller."),
                            ("Boundary and loop: the two things we build",
                             "A fail-closed telemetry gate that rejects a non-conforming batch rather than dropping fields, and a governed operator loop that audits every action it takes.")]):
    cx = 0.29 + i * 4.8
    c = rect(s, cx, 3.1, 4.62, 1.25, fill=PANEL, line=None, radius=0.05)
    tf = c.text_frame
    p = tf.paragraphs[0]; r = p.add_run(); r.text = t
    r.font.size = Pt(9.5); r.font.bold = True; r.font.color.rgb = INK; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(3); r = p.add_run(); r.text = d
    r.font.size = Pt(8); r.font.color.rgb = MUTED; r.font.name = BODY
rich(s, 0.29, 4.55, 9.42, 0.4, [[("EU AI Act Art. 10 / 12 / 14 / 15 · GDPR Art. 25 / 32 / 44–49 · ISO/IEC 42001 · 27001 · SOC 2 Type II · HIPAA §164.312", {"color": MUTED})]], size=8.5)
rich(s, 0.29, 4.9, 9.42, 0.3, [[("We don't certify anyone. We generate the evidence a certification asks for, continuously.", {"bold": True})]], size=9)

# ---------------------------------------------------------------- 11 · architecture 2
s = new_slide("Architecture diagram of the proposed solution", 11)
head(s, "What leaves the building, exactly")
rows = [("Class", "Leaves?", "Contents"),
        ("C0", "Yes, allowlisted", "Hardware and OS health: CPU, RAM, disk, GPU, inference liveness, instance inventory, rule metrics"),
        ("C1", "Yes, transformed", "Salted hashes, bounded display labels, timestamps. Never raw identifiers"),
        ("C2", "Never", "Workload payloads, call transcripts, patient or customer records, prompts, credentials, logs")]
tbl = s.shapes.add_table(4, 3, Inches(0.29), Inches(1.9), Inches(9.42), Inches(1.75)).table
light_table(tbl)
for w, i in zip([0.9, 1.7, 6.82], range(3)):
    tbl.columns[i].width = Inches(w)
for ri, row in enumerate(rows):
    for ci, val in enumerate(row):
        cell = tbl.cell(ri, ci)
        cell.margin_left = Inches(0.08); cell.margin_top = Inches(0.03); cell.margin_bottom = Inches(0.03)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        cell.fill.solid(); cell.fill.fore_color.rgb = WHITE if ri == 0 else (PANEL if ri % 2 else WHITE)
        p = cell.text_frame.paragraphs[0]
        r = p.add_run(); r.text = val
        r.font.size = Pt(8.5 if ri else 8); r.font.name = BODY
        r.font.bold = (ri == 0) or (ci == 0)
        r.font.color.rgb = MUTED if ri == 0 else (RED if (ci == 1 and val == "Never") else INK)
for i, (t, d) in enumerate([("The one external call, disclosed",
                             "The operator's diagnosis runs on Gemini via Vertex AI. The prompt is C0 only, the audit row records egress=external, and one variable (backend=local) puts it back on the node's own weights."),
                            ("Why publish the exception",
                             "A boundary you cannot see is not a boundary. The claim is “nothing C2 leaves, and everything that does is named, classified and audited.”")]):
    cx = 0.29 + i * 4.8
    c = rect(s, cx, 3.85, 4.62, 1.2, fill=PANEL, line=None, radius=0.05)
    tf = c.text_frame
    p = tf.paragraphs[0]; r = p.add_run(); r.text = t
    r.font.size = Pt(9.5); r.font.bold = True; r.font.color.rgb = INK; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(3); r = p.add_run(); r.text = d
    r.font.size = Pt(8); r.font.color.rgb = MUTED; r.font.name = BODY

# ---------------------------------------------------------------- 12 · technologies
s = new_slide("Technologies to be used in the solution", 12)
head(s, "Gemini decides. Gemma stays home.")
techs = [("Gemini 2.5 Flash · Vertex AI", "The diagnosis: naming the remediation from C0 telemetry. Measured 1.8 s warm. The only call that leaves the premises, and it says so in the audit trail."),
         ("Gemma 3 · on the node", "Google's open weights, served by llama.cpp. Automatically detects the inference node, bootstraps the toolchain and leaves it ready for agents, with nothing forwarded."),
         ("Cloud Run · GPU reference", "The same Gemma on 1× L4 with vLLM, deployed as the managed reference. It is the shape this takes when GPU quota stops being the blocker.")]
for i, (t, d) in enumerate(techs):
    cx = 0.29 + i * 3.18
    c = rect(s, cx, 2.25, 3.02, 1.62, fill=PANEL, line=None, radius=0.06)
    tf = c.text_frame
    tf.margin_left = Inches(0.12)
    p = tf.paragraphs[0]; r = p.add_run(); r.text = t
    r.font.size = Pt(10.5); r.font.bold = True; r.font.color.rgb = INK; r.font.name = BODY
    p = tf.add_paragraph(); p.space_before = Pt(4); r = p.add_run(); r.text = d
    r.font.size = Pt(8.2); r.font.color.rgb = MUTED; r.font.name = BODY
rich(s, 0.29, 4.05, 9.42, 0.35, [[("GCE controller · GCE node · Firebase Hosting · Cloud Storage (weights) · pre-baked GCE image · LXD · llama.cpp", {"color": MUTED})]], size=8.5)
rich(s, 0.29, 4.55, 9.42, 0.5, [[("Local data uses local models. ", {"bold": True}),
                                 ("Where the customer permits it, Termini integrates with Gemini.", {"color": BLUE, "bold": True})]], size=12)

# ---------------------------------------------------------------- 13 · benchmarking
s = new_slide("Prototype Performance report/Benchmarking", 13)
head(s, "What runs today, measured")
metrics = [("150 ms", "Node registration from the pre-baked image, with zero installs at boot", INK),
           ("0", "C2 payloads the controller has ever stored. Fail-closed by design.", GREEN),
           ("422", "A C2 field rejected before any byte is stored, checked three ways", RED),
           ("1", "Remediation decided by the model, executed and audited end to end", INK),
           ("1.8 s", "Diagnosis warm on the managed path. 402 s on local CPU weights, stated not hidden.", INK),
           ("live", "On GCE + Firebase + Vertex AI. A judge can open it and watch.", BLUE)]
for i, (v, k, col) in enumerate(metrics):
    cx = 0.29 + (i % 3) * 3.18
    cy = 1.95 + (i // 3) * 1.25
    txt(s, cx, cy, 3.0, 0.45, v, size=26, bold=True, color=col)
    txt(s, cx, cy + 0.48, 2.95, 0.6, k, size=8.5, color=MUTED, spacing=1.15)
rich(s, 0.29, 4.62, 9.42, 0.4, [[("Three claims, six measurements. ", {"color": MUTED}), ("Nothing on this page is a projection.", {"bold": True})]], size=10)

# ---------------------------------------------------------------- 14 · snapshots
s = new_slide("Snapshots of the prototype", 14)
head(s, "The operator, on screen")
s.shapes.add_picture(SHOT, Inches(0.29), Inches(1.8), width=Inches(6.55), height=Inches(3.53))
rich(s, 7.0, 1.85, 2.7, 3.4, [
    [("Operator log", {"bold": True, "size": 10})],
    [("Every incident: the rule that fired, the model's reasoning, the evidence fields, the action executed and the audit id.", {"size": 8.2, "color": MUTED})],
    [("Fallback visible", {"bold": True, "size": 10})],
    [("Decisions the model did not take are labelled deterministic or fallback, in the same list.", {"size": 8.2, "color": MUTED})],
    [("Live", {"bold": True, "size": 10})],
    [("termini-510609.web.app. The demo is paused between judging sessions; the resume command is in SUBMISSION.md §3.", {"size": 8.2, "color": MUTED})]])

# ---------------------------------------------------------------- 15 · future
s = new_slide("Additional Details/Future Development (if any)", 15)
head(s, "From one clinic to a fleet of them")
fut = [("Real bare-metal onboarding", "MAAS + iPXE netboot, so a box arrives from a partner and joins without a technician on site."),
       ("A GPU node in the demo", "L4 quota is the current blocker; the path is already built and served on Cloud Run."),
       ("Learned baselines", "Edge anomaly detection replacing static thresholds, so the operator learns each site's normal."),
       ("Federated eval exchange", "Evals and weights travel between entities; records don't. The plural-ecosystem mechanism, productised."),
       ("Compliance mapping", "GDPR, EU AI Act, Korea PIPA, India DPDP posture mapped onto the C0/C1/C2 boundary."),
       ("Hardware partner channel", "The box is commodity; the operator is the product. Partners sell metal, we run it.")]
for i, (t, d) in enumerate(fut):
    cx = 0.29 + (i % 2) * 4.8
    cy = 1.95 + (i // 2) * 1.0
    txt(s, cx, cy, 4.5, 0.25, t, size=10, bold=True)
    txt(s, cx, cy + 0.26, 4.5, 0.6, d, size=8.2, color=MUTED, spacing=1.15)

# ---------------------------------------------------------------- 16 · links
s = new_slide("Provide links to your:", 16)
head(s, "Links")
FULL = ("https://termini-510609.web.app/?apiBase=https%3A%2F%2F34-93-190-64.sslip.io%2Fv1"
        "&projectId=01J0DEM0WARM000000000000AB&synthetic=1&dest=fleet")

txt(s, 0.29, 1.95, 2.6, 0.3, "GitHub public repository", size=10.5, bold=True)
box = txt(s, 2.95, 1.95, 6.7, 0.3, "github.com/yanksyoon/termini", size=10, color=BLUE)
link_runs(box, "https://github.com/yanksyoon/termini")

txt(s, 0.29, 2.5, 2.6, 0.3, "Final product link", size=10.5, bold=True)
txt(s, 0.29, 2.82, 9.42, 0.25,
    "opens the full dashboard: fleet, operator log, agents panel, synthetic nodes included",
    size=8, color=MUTED)
p1 = "https://termini-510609.web.app/?apiBase=https%3A%2F%2F34-93-190-64.sslip.io%2Fv1"
p2 = "&projectId=01J0DEM0WARM000000000000AB&synthetic=1&dest=fleet"
box = txt(s, 2.95, 3.12, 6.7, 0.55, p1 + "\n" + p2, size=8, color=BLUE, spacing=1.35)
link_runs(box, FULL)

txt(s, 0.29, 3.95, 2.6, 0.3, "Demo sign-in", size=10.5, bold=True)
txt(s, 2.95, 3.95, 6.7, 0.3, "project token: demo-project-token", size=10, color=INK)
txt(s, 2.95, 4.24, 6.7, 0.3,
    "the dashboard refuses a token inside a URL, so the token is printed here; paste it at Settings, then sign in",
    size=8, color=MUTED)

txt(s, 0.29, 4.72, 2.6, 0.3, "Live API", size=10.5, bold=True)
box = txt(s, 2.95, 4.72, 6.7, 0.3, "34-93-190-64.sslip.io/v1", size=10, color=BLUE)
link_runs(box, "https://34-93-190-64.sslip.io/v1")

# ---------------------------------------------------------------- closing image, rebuilt
sc = prs.slides.add_slide(BLANK)
sc.shapes.add_picture(CLOSING, 0, 0, width=Inches(10), height=Inches(5.625))
txt(sc, 5.3, 2.32, 4.3, 0.9, "The doctor built it alone.\nThe next one won't have to.",
    size=15, bold=True, color=WHITE, spacing=1.25)
txt(sc, 5.3, 3.12, 4.3, 0.3, "Your hardware. Your data. Our operations.",
    size=10, color=RGBColor(0xBD, 0xC1, 0xC6))

for _s in prs.slides:
    normalise(_s)

prs.save(OUT)
print("saved", OUT, "slides:", len(prs.slides._sldIdLst))
