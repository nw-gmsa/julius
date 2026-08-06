"""
Renders the /data-quality report (the dict shape IrisClient.build_report()
in iris_client.py returns) to a downloadable PDF, via reportlab. The only
place in this app that *generates* a PDF, as opposed to serving one
straight off the FHIR server (see fetch_attachment_bytes() in
fhir_client.py for that — a report's presentedForm PDF, already made by
iGene). Kept in its own module for the same reason iris_client.py is:
app.py stays Flask routes only.

Mirrors data_quality.html's structure section for section (row count,
"Why, overall", "By source", one section per source) so the PDF download
matches what's on screen for the same start/end/score_threshold — see
app._build_data_quality_report(), shared by both the HTML and PDF routes
so they can never drift apart on what "the report" actually contains.
"""
import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_STYLES = getSampleStyleSheet()

#: One table look used throughout — a shaded, bold header row repeated
#: on every page a table spans, light grid lines, small font so wider
#: entry tables (up to 8+ columns) still fit landscape A4.
_TABLE_STYLE = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
])


def _para(text, style="Normal"):
    """A Paragraph flowable — `text` may contain reportlab's mini-XML
    markup (e.g. `<b>`), so callers building text from data (column
    names, error messages) must escape() it themselves first; this
    function doesn't, since some callers deliberately pass markup."""
    return Paragraph(text, _STYLES[style])


def _cell(value):
    """Plain-text table cell — never wrapped in a Paragraph, so this
    never needs escaping: reportlab's Table only interprets markup for
    Flowable cell content (Paragraph, etc.), not plain strings."""
    return "—" if value is None else str(value)


def _table(rows):
    t = Table(rows, repeatRows=1)
    t.setStyle(_TABLE_STYLE)
    return t


def quality_report_pdf_bytes(report, start, end, score_threshold, iris_host, iris_namespace, error=None):
    """Build the PDF for the current /data-quality report and return its
    bytes. Handles every shape build_report() (and its wrapping
    try/except in app.py) can produce — a caught exception (`error` set,
    `report` None), a report with its own top-level "error" (table not
    found), a report whose quality section itself failed
    (`quality["ok"] is False`), and a report with no quality section at
    all (`report["quality"] is None`, e.g. no source/score column found)
    — each stopping the PDF after explaining why, same as
    data_quality.html's equivalent branches.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
        title="Data quality report",
    )
    story = [
        _para("Data quality report", "Title"),
        _para(f"{escape(iris_host or '')} &mdash; namespace {escape(iris_namespace or '')}"),
        _para(f"Last updated between {escape(start or '')} and {escape(end or '')}. Low score threshold &le; {score_threshold}."),
        Spacer(1, 10),
    ]

    if error:
        story.append(_para(f"<b>Request failed:</b> {escape(error)}"))
        doc.build(story)
        return buffer.getvalue()

    if not report:
        story.append(_para("No report data."))
        doc.build(story)
        return buffer.getvalue()

    if report.get("date_filtered"):
        story.append(_para(f"Filtered to rows where {escape(report['last_updated_column'])} falls in the range above."))
    else:
        story.append(_para(
            "No last-updated-like column was recognised by name on this table, "
            "so the date range above has no effect."
        ))
    story.append(_para(f"{report.get('total_rows', 0)} row(s) in range."))
    story.append(Spacer(1, 10))

    quality = report.get("quality")
    if not quality:
        story.append(_para(
            "No source-like and score-like column were both recognised by name "
            "on this table, so the quality section couldn't run."
        ))
        doc.build(story)
        return buffer.getvalue()

    if not quality.get("ok"):
        story.append(_para(f"<b>Quality section failed:</b> {escape(quality.get('error') or '')}"))
        doc.build(story)
        return buffer.getvalue()

    story.append(_para("Quality", "Heading1"))
    story.append(_para(escape(quality["summary"])))
    if quality.get("reason_columns_detected"):
        story.append(_para("Reasons recognised on this table: " + escape(", ".join(quality["reason_columns_detected"])) + "."))
    story.append(Spacer(1, 10))

    if quality.get("reason_totals"):
        story.append(_para("Why, overall", "Heading2"))
        rows = [["Reason", "Rows"]] + [[r["reason"], _cell(r["count"])] for r in quality["reason_totals"]]
        story.append(_table(rows))
        story.append(Spacer(1, 10))

    if quality.get("entries_by_source"):
        story.append(_para("By source", "Heading2"))
        rows = [["Source (ODS code)", "Unique low/null-score rows"]] + [
            [g["source"], _cell(g["count"])] for g in quality["entries_by_source"]
        ]
        story.append(_table(rows))
        story.append(Spacer(1, 10))

    if quality.get("truncated"):
        story.append(_para(
            "The underlying fetch this report is built from is capped &mdash; some "
            "sources may have more unique rows than shown above and in the listings below."
        ))
        story.append(Spacer(1, 10))

    display_columns = quality.get("display_columns", [])
    for group in quality["entries_by_source"]:
        story.append(PageBreak())
        story.append(_para(f"{escape(group['source'])} ({group['count']})", "Heading2"))
        if group.get("reason_totals"):
            story.append(_para(escape("; ".join(f"{r['reason']}: {r['count']}" for r in group["reason_totals"]))))
            story.append(Spacer(1, 6))
        rows = [display_columns + ["Reasons"]]
        for entry in group["entries"]:
            row = [_cell(entry.get(c)) for c in display_columns]
            row.append(", ".join(entry.get("reasons") or []) or "—")
            rows.append(row)
        story.append(_table(rows))

    doc.build(story)
    return buffer.getvalue()
