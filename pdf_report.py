"""
Renders the /data-quality report (the dict shape IrisClient.build_report()
in iris_client.py returns) and the /stats/monthly-reports report (see
monthly_report_pdf_bytes()) to downloadable PDFs, via reportlab. The only
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
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_STYLES = getSampleStyleSheet()
_CELL_STYLE = ParagraphStyle("TableCell", parent=_STYLES["Normal"], fontSize=8, leading=10)

#: Page geometry, shared between the SimpleDocTemplate margins below and
#: the entries table's column widths (which need to know how much
#: horizontal space they actually have to work with).
_PAGE_MARGIN = 15 * mm
_CONTENT_WIDTH = landscape(A4)[0] - 2 * _PAGE_MARGIN

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
    Flowable cell content (Paragraph, etc.), not plain strings. Only
    safe for short, single-value content — see _reasons_cell() for
    content that needs to wrap."""
    return "—" if value is None else str(value)


def _reasons_cell(reasons):
    """A row's reasons as a wrapping Paragraph, one reason per line
    (`<br/>` between them) rather than a single comma-joined plain
    string — a plain string doesn't wrap at all in a Table cell, so a
    row with several reasons produced one very long line that ran past
    the column (and, since the entries table had no explicit column
    widths, could blow out the whole table's layout along with it).
    Escaped since Paragraph text is interpreted as reportlab markup."""
    if not reasons:
        return _cell(None)
    return Paragraph("<br/>".join(escape(r) for r in reasons), _CELL_STYLE)


def _table(rows):
    t = Table(rows, repeatRows=1)
    t.setStyle(_TABLE_STYLE)
    return t


def _entries_table(display_columns, rows):
    """The per-source entries table — unlike _table(), this always gets
    explicit column widths: a fixed width per identifying column
    (NHS number/MRN/surname/etc. — short values), with whatever's left
    of the page going to Reasons, which is the one column that actually
    needs room to wrap (see _reasons_cell()). Without explicit widths,
    reportlab sizes each column to fit its widest *unwrapped* content,
    which is exactly what let a long reasons list distort the table."""
    id_col_width = 65
    reasons_width = max(_CONTENT_WIDTH - len(display_columns) * id_col_width, 120)
    col_widths = [id_col_width] * len(display_columns) + [reasons_width]
    t = Table(rows, colWidths=col_widths, repeatRows=1)
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
            row.append(_reasons_cell(entry.get("reasons")))
            rows.append(row)
        story.append(_entries_table(display_columns, rows))

    doc.build(story)
    return buffer.getvalue()


def _name_cell(text, indent=0):
    """A wrapping name cell (ICS/trust/category names can be long) —
    escaped, since Paragraph text is interpreted as reportlab markup."""
    style = ParagraphStyle("NameCell", parent=_CELL_STYLE, leftIndent=indent)
    return Paragraph(escape(text or "—"), style)


def _category_body(rows):
    """Category table body rows (+ the indexes of sub-category rows, for
    _count_table()'s indented styling) and the top-level-only per-month
    totals — sub-categories are already counted in their parent row."""
    body, child_rows = [], []
    for r in rows:
        if r.get("child"):
            child_rows.append(len(body))
        body.append([_name_cell(r["label"], indent=10 if r.get("child") else 0)] + r["months"] + [r["total"]])
    n_months = len(rows[0]["months"]) if rows else 0
    totals = [sum(r["months"][i] for r in rows if not r.get("child")) for i in range(n_months)]
    return body, child_rows, totals


def _count_table(header, body_rows, total_row, name_width, extra_widths=(), trust_rows=()):
    """A name + counts table (name column, any `extra_widths` columns,
    then one column per month plus Total) with a bold total row. Months
    share whatever width is left. `trust_rows` are body row indexes
    (0-based, header excluded) drawn as shaded sub-rows (trusts under an
    ICS, sub-categories under a category)."""
    n_counts = len(header) - 1 - len(extra_widths)
    count_width = max((_CONTENT_WIDTH - name_width - sum(extra_widths)) / max(n_counts, 1), 28)
    col_widths = [name_width, *extra_widths] + [count_width] * n_counts
    rows = [header] + body_rows + ([total_row] if total_row else [])
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    style = list(_TABLE_STYLE.getCommands())
    style.append(("ALIGN", (1 + len(extra_widths), 0), (-1, -1), "RIGHT"))
    if total_row:
        style += [("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                  ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f0f0f0"))]
    for i in trust_rows:
        style += [("BACKGROUND", (0, i + 1), (-1, i + 1), colors.HexColor("#fafbfd")),
                  ("TEXTCOLOR", (0, i + 1), (-1, i + 1), colors.HexColor("#444444"))]
    t.setStyle(TableStyle(style))
    return t


def monthly_report_pdf_bytes(report):
    """Build the PDF for the /stats/monthly-reports page and return its
    bytes. `report` is app._build_monthly_report()'s context dict — the
    same one the HTML page renders, so the two always match. Sections
    follow the page (coverage note, warnings, reports by category with a
    total, reports by ICS, the Unknown ICS's requesting organisation
    codes), except that what the page hides behind clicks
    is written out in full: every ICS's trusts are listed under it, and a
    final "Categories by ICS" section gives each ICS's category breakdown
    (the page's Categories popup)."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        leftMargin=_PAGE_MARGIN, rightMargin=_PAGE_MARGIN, topMargin=_PAGE_MARGIN, bottomMargin=_PAGE_MARGIN,
        title="Monthly reports",
    )
    months = report.get("months") or []
    region = report.get("selected_region")
    category_label = report.get("selected_category_label")
    span = f"{months[0]} to {months[-1]}" if len(months) > 1 else (months[0] if months else "—")
    filters = [f"Months: {span}", f"NHS region: {region or 'All regions'}",
               f"Category: {category_label or 'All categories'}"]
    story = [
        _para("Monthly reports", "Title"),
        _para(" &middot; ".join(escape(f) for f in filters)),
        _para("Genomic test reports per calendar month, by the ICS of the <i>ordering</i> organisation. "
              "<b>Coverage:</b> reports from iGene and Shire only &mdash; StarLIMS reports aren't included."),
        Spacer(1, 8),
    ]

    if report.get("error"):
        story.append(_para(f"<b>Request failed:</b> {escape(report['error'])}"))
    if report.get("truncated_months"):
        story.append(_para(
            f"<b>Hit the fetch limit</b> (searched a week at a time, up to {report.get('max_pages')} pages per week) "
            f"for {escape(', '.join(report['truncated_months']))} &mdash; counts may be incomplete."))
    if not months:
        doc.build(story)
        return buffer.getvalue()

    month_header = list(months) + ["Total"]

    story.append(_para(f"Reports by category{escape(' — ' + region) if region else ''}", "Heading2"))
    body, child_rows, totals = _category_body(report.get("category_rows") or [])
    totals = totals or [0] * len(months)
    story.append(_count_table(
        ["Category"] + month_header, body, ["Total"] + totals + [sum(totals)],
        name_width=200, trust_rows=child_rows,
    ))

    delivery = report.get("delivery")
    if delivery:
        suffix = (f" — {region}" if region else "") + (f" — {category_label}" if category_label else "")
        delivery_heading = [
            _para(escape("Reports actually delivered" + suffix), "Heading2"),
            _para("Reports whose ordering trust receives them electronically (live trusts are maintained by hand in app.py)."),
        ]
        rows = [["Status", "Reports", "% of total", "Trusts"]]
        for st in delivery["statuses"]:
            trusts = ", ".join(st["trusts"]) if st["trusts"] else ("All other trusts" if st["label"] == "Not live" else "—")
            rows.append([st["label"], st["reports"], f"{st['pct']}%", _name_cell(trusts)])
        rows.append(["Total", delivery["total"], "100%", ""])
        t = Table(rows, colWidths=[110, 70, 70, _CONTENT_WIDTH - 250], repeatRows=1)
        t.setStyle(TableStyle(list(_TABLE_STYLE.getCommands()) + [
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f0f0f0")),
        ]))
        story.append(KeepTogether(delivery_heading + [t]))
        if delivery["targets"]:
            story.append(Spacer(1, 8))
            story.append(_para(
                f"<b>Recommended targets</b> &mdash; top {len(delivery['targets'])} trusts by volume, not yet live or "
                f"nearing go-live: taking these live would deliver the most extra reports."))
            rows = [["#", "NHS trust", "ICS", "Reports", "% of total"]]
            for i, tg in enumerate(delivery["targets"], 1):
                rows.append([i, _name_cell(tg["name"]), _name_cell(tg["ics"]), tg["reports"], f"{tg['pct']}%"])
            t = Table(rows, colWidths=[25, _CONTENT_WIDTH - 350, 170, 70, 85], repeatRows=1)
            t.setStyle(_TABLE_STYLE)
            story.append(t)

    heading = "Reports by ICS" + (f" — {region}" if region else "") + (f" — {category_label}" if category_label else "")
    story.append(_para(escape(heading), "Heading2"))
    show_region = not region
    body, trust_rows = [], []
    for row in report.get("ics_rows") or []:
        # Same rows the page shows: every ICS in a region view; in the
        # all-regions view, current ICSs plus anything else with reports.
        if not (region or row["total"] or row["region"] != "—"):
            continue
        body.append([_name_cell(row["label"])] + ([row["region"]] if show_region else []) + row["months"] + [row["total"]])
        for t in row["trusts"]:
            trust_rows.append(len(body))
            body.append([_name_cell(t["name"], indent=10)] + ([""] if show_region else []) + t["months"] + [t["total"]])
    month_totals = report.get("month_totals") or [0] * len(months)
    story.append(_count_table(
        ["ICS / NHS trust"] + (["Region"] if show_region else []) + month_header,
        body,
        ["Total"] + ([""] if show_region else []) + month_totals + [sum(month_totals)],
        name_width=230, extra_widths=(105,) if show_region else (), trust_rows=trust_rows,
    ))

    if report.get("patient_inferred_reports"):
        n = report["patient_inferred_reports"]
        story.append(_para(
            f"{n} report{'s' if n != 1 else ''} with no link to an order (no basedOn) placed by the patient's "
            f"other orders &mdash; counted only where all of them came from the same trust."))

    unknown_rows = report.get("unknown_code_rows") or []
    if unknown_rows:
        heading = "Unknown ICS — requesting organisation codes provided" + (
            f" — {category_label}" if category_label else "")
        story.append(_para(escape(heading), "Heading2"))
        story.append(_para(
            "Reports counted under Unknown above, by the requesting organisation code each one carried "
            "(the ODS code on the report or its order, else the order requester's identifier or reference) "
            "&mdash; or, where there was none, why."))
        unknown_totals = report.get("unknown_code_month_totals") or [0] * len(months)
        # The three text columns share what the count columns (at least 32pt
        # each) leave, so a long month range still fits the page.
        text_width = max(_CONTENT_WIDTH - 32 * len(month_header), 210)
        story.append(_count_table(
            ["Code provided", "Organisation name given", "Where it came from"] + month_header,
            [[_name_cell(r["code"] or "(none)"), _name_cell(r["name"] or "—"), _name_cell(r["source"])]
             + r["months"] + [r["total"]] for r in unknown_rows],
            ["Total", "", ""] + unknown_totals + [sum(unknown_totals)],
            name_width=text_width * .25, extra_widths=(text_width * .35, text_width * .4),
        ))

    suffix = (f" — {region}" if region else "") + (f" — {category_label}" if category_label else "")
    for i, wo in enumerate(report.get("work_order_sections") or []):
        story.append(_para(escape(wo["title"] + suffix), "Heading2"))
        if wo.get("direction") == "incoming":
            story.append(_para(
                "dWGS sub-orders sent to the NW GLH by NE&amp;Y Genomics' laboratories (699C0, 699N0, 699D0), under "
                "the trust on the NGIS referral. By month dispatched (authoredOn); orders without one by month last updated."))
        if i == 0:
            story.append(_para(
                "StarLIMS and Omics DSS reports don't come through the RIE yet, so these are the work orders sent "
                "to each (ServiceRequests whose performer is that system), by month authored, under the trust that "
                "placed the original order. Categories from the order's test code. Not added to the report "
                "figures above."))
        if wo.get("error"):
            story.append(_para(f"<b>{escape(wo['error'])}</b>"))
            continue
        if not sum(wo.get("month_totals") or []):
            story.append(_para("None in the months shown."))
            continue
        if wo.get("truncated_months"):
            story.append(_para("<b>Hit the fetch limit for " + escape(", ".join(wo["truncated_months"]))
                               + "</b> &mdash; counts may be incomplete."))
        body, child_rows, totals = _category_body(wo.get("category_rows") or [])
        totals = totals or [0] * len(months)
        story.append(_count_table(
            ["Category"] + month_header, body, ["Total"] + totals + [sum(totals)],
            name_width=200, trust_rows=child_rows,
        ))
        story.append(Spacer(1, 8))
        body, trust_rows = [], []
        for row in wo.get("ics_rows") or []:
            if not row["total"]:
                continue
            body.append([_name_cell(row["label"])] + ([row["region"]] if show_region else []) + row["months"] + [row["total"]])
            for t in row["trusts"]:
                trust_rows.append(len(body))
                body.append([_name_cell(t["name"], indent=10)] + ([""] if show_region else []) + t["months"] + [t["total"]])
        wo_totals = wo.get("month_totals") or [0] * len(months)
        story.append(_count_table(
            ["ICS / NHS trust"] + (["Region"] if show_region else []) + month_header,
            body,
            ["Total"] + ([""] if show_region else []) + wo_totals + [sum(wo_totals)],
            name_width=230, extra_widths=(105,) if show_region else (), trust_rows=trust_rows,
        ))

    dq_rows = report.get("data_quality_rows") or []
    story.append(_para("Data quality issues", "Heading2"))
    if dq_rows:
        story.append(_para("Problems in the data behind this report, for every report in the months shown "
                           "(the region and category filters don't apply)."))
        text_width = max(_CONTENT_WIDTH - 32 * len(month_header), 210)
        story.append(_count_table(
            ["Issue", "Value", "Where"] + month_header,
            [[Paragraph(f"<b>{escape(r['issue'])}</b><br/>{escape(r['fix'])}", _CELL_STYLE),
              _name_cell(r["detail"] or "—"), _name_cell("; ".join(r["where"]))] + r["months"] + [r["total"]]
             for r in dq_rows],
            None,
            name_width=text_width * .5, extra_widths=(text_width * .2, text_width * .3),
        ))
    else:
        story.append(_para("None found in the months shown."))

    breakdowns = [r for r in (report.get("ics_rows") or []) if r.get("categories")]
    if breakdowns:
        story.append(PageBreak())
        story.append(_para("Categories by ICS", "Heading2"))
        story.append(_para("Each ICS's reports by category (all categories, whatever the category filter)."))
        for row in breakdowns:
            body, child_rows, cat_totals = _category_body(row["categories"])
            # Heading + table kept on one page — a couple of dozen short rows at most.
            story.append(KeepTogether([
                Spacer(1, 6),
                _para(f"<b>{escape(row['name'])}</b>"),
                _count_table(
                    ["Category"] + month_header, body, ["Total"] + cat_totals + [sum(cat_totals)],
                    name_width=200, trust_rows=child_rows,
                ),
            ]))

    doc.build(story)
    return buffer.getvalue()


def validation_report_pdf_bytes(report, job):
    """The /testing/send-message FHIR validation report
    (fhir_validator.build_report()) as a landscape-A4 PDF: a summary
    table, then one section per validated resource listing its errors and
    warnings (and information, if included). All data goes through
    escape() since every cell here is a wrapping Paragraph."""
    import time as _time
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=_PAGE_MARGIN, rightMargin=_PAGE_MARGIN,
                            topMargin=_PAGE_MARGIN, bottomMargin=_PAGE_MARGIN,
                            title="Data security report - FHIR validation")

    def p(text):
        return Paragraph(escape(text or ""), _CELL_STYLE)

    story = [_para("Data security report", "Title"), _para("FHIR validation", "Heading2")]
    result_colours = {"fail": ("#fdecee", "#8a1020"), "incomplete": ("#fff4e5", "#7a4a00"),
                      "pass_with_warnings": ("#fff8e1", "#6b5200"), "pass": ("#e8f5ec", "#135c2c")}
    bg, fg = result_colours.get(report.get("result"), ("#f0f0f0", "#000000"))
    banner_style = ParagraphStyle("ResultBanner", parent=_STYLES["Normal"], fontSize=11, leading=15,
                                  textColor=colors.HexColor(fg))
    banner = Table([[Paragraph(f"<font size=16><b>Result: {escape(report.get('result_label') or '')}</b></font>"
                               f"<br/>{escape((report.get('result_reason') or '').capitalize())}.", banner_style)]],
                   colWidths=[_CONTENT_WIDTH])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg)),
                                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor(fg)),
                                ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [banner, Spacer(1, 4 * mm)]
    totals = report["totals"]
    summary = (f"<b>{totals['error']} error(s)</b>, <b>{totals['warning']} warning(s)</b>"
               + (f", {totals['information']} information" if report["include_information"] else "")
               + f" across {len(report['sections'])} validated resource(s).")
    story.append(_para(summary))
    details = []
    if job.get("filename"):
        details.append(f"File: {job['filename']}")
    if job.get("v2"):
        details.append("Input: HL7 v2 message, converted to FHIR by the ESB; issues are shown against its "
                       "segments and fields (line = segment position, MSH = 1)")
    details.append(f"Validated: {_time.strftime('%Y-%m-%d %H:%M', _time.localtime(job.get('started') or _time.time()))}")
    details.append(f"Terminology server: {job.get('tx')}")
    details.append("Shown: errors and warnings" + (" and information" if report["include_information"] else ""))
    if report["ignored_total"]:
        details.append(f"Ignored (message contains): {'; '.join(report['ignore'])}"
                       f" — {report['ignored_total']} issue(s) left out")
    story.append(_para("<br/>".join(escape(d) for d in details)))
    if report.get("advice"):
        story.append(_para("Tackling the issues", "Heading3"))
        story.append(_para("<br/>".join(f"{i}. {escape(a)}" for i, a in enumerate(report["advice"], 1))))
    story.append(Spacer(1, 6 * mm))

    info = report["include_information"]
    header = ["Resource", "Validated against", "Errors", "Warnings"] + (["Info"] if info else [])
    rows = [header]
    for s in report["sections"]:
        label = s["label"] + ((f" (automatic, stands in for {len(s['represents'])} more)" if s.get("represents")
                               else " (automatic)") if s.get("auto") else "") + (" (before later edits)" if s.get("stale") else "")
        rows.append([p(label), p(s.get("profile") or "base resource + meta.profile"),
                     "failed" if s.get("error") else str(s["counts"]["error"]), str(s["counts"]["warning"])]
                    + ([str(s["counts"]["information"])] if info else []))
    count_w = 45
    widths = [_CONTENT_WIDTH * 0.45, _CONTENT_WIDTH - _CONTENT_WIDTH * 0.45 - count_w * (3 if info else 2)] \
        + [count_w] * (3 if info else 2)
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(_TABLE_STYLE)
    story.append(t)

    sev_colours = {"fatal": "#b00020", "error": "#b00020", "warning": "#a15c00", "information": "#555555"}
    main_story = story
    main_story.append(Spacer(1, 6 * mm))
    issues_heading = _para("Issues by resource", "Heading2")
    issues_heading.keepWithNext = 1
    main_story.append(issues_heading)
    for s in report["sections"]:
        if s is not report["sections"][0]:
            main_story.append(Spacer(1, 4 * mm))
        story = []  # this section's heading and notes, kept with the start of its table
        story.append(_para(escape(s["label"]), "Heading3"))
        meta = (["Validated automatically - named in \"Unable to find a profile match\""] if s.get("auto") else []) + \
            [f"Against {s.get('profile') or 'base resource + meta.profile'} ({s.get('ig_label') or ''})"]
        if s.get("bundle_rule_text"):
            meta.append(f"With {s['bundle_rule_text']}")
        if s.get("conversion_note"):
            meta.append(s["conversion_note"])
        if s.get("represents"):
            meta.append(f"Stands in for {len(s['represents'])} other {s.get('resource_type')}(s) named in the same "
                        f"errors, not validated individually - likely to have the same problems: "
                        f"{', '.join(s['represents'])}")
        if s.get("stale"):
            meta.append("NOTE: validated before later edits to this resource - re-validate for current results")
        if s.get("ignored"):
            meta.append(f"{s['ignored']} ignored issue(s) not shown")
        story.append(Paragraph("<br/>".join(escape(m) for m in meta), _CELL_STYLE))
        story.append(Spacer(1, 2 * mm))
        if s.get("error"):
            story.append(_para(f"<b>Validation failed:</b> {escape(s['error'])}"))
            main_story.append(KeepTogether(story))
            continue
        if not s["groups"]:
            story.append(_para("No issues."))
            main_story.append(KeepTogether(story))
            continue
        has_entries = s.get("has_entries")
        has_v2 = s.get("has_v2")
        rows = [["Severity"] + (["HL7 v2"] if has_v2 else []) + ["FHIR location" if has_v2 else "Location", "Message"]
                + (["Where"] if has_entries else [])]
        for g in s["groups"]:
            sev = Paragraph(f'<font color="{sev_colours.get(g["severity"], "#000000")}"><b>'
                            f'{escape(g["severity"])}</b></font>', _CELL_STYLE)
            location = g.get("location") or "(resource)"
            if not has_entries and g["occurrences"][0].get("line"):
                location += f" (line {g['occurrences'][0]['line']})"
            message = escape(g.get("message") or "")
            for _, label in g.get("see") or []:
                message += f"<br/>&gt;&gt; why: see {escape(label)} below"
            if g.get("mismatch_note"):
                message += f"<br/>&gt;&gt; {escape(g['mismatch_note'])}"
            row = [sev]
            if has_v2:
                v2 = g.get("v2") or {}
                lines = []
                for o in g["occurrences"]:
                    line = (o.get("v2") or {}).get("line")
                    if line and line not in lines:
                        lines.append(line)
                text = escape(v2.get("text") or "-")
                text = f"<i>{text}</i>" if v2.get("conversion") else f"<b>{text}</b>"
                if lines:
                    text += "<br/>" + escape("line " + ", ".join(str(n) for n in lines))
                row.append(Paragraph(text, _CELL_STYLE))
            row += [p(location), Paragraph(message, _CELL_STYLE)]
            if has_entries:
                row.append(Paragraph("<br/>".join(
                    escape((o.get("entry_label") or o.get("location") or "(resource)")
                           + (f" (line {o['line']})" if o.get("line") else ""))
                    for o in g["occurrences"]), _CELL_STYLE))
            rows.append(row)
        sev_w, loc_w, where_w, v2_w = 50, 170, (200 if has_entries else 0), (120 if has_v2 else 0)
        widths = [sev_w] + ([v2_w] if has_v2 else []) + [loc_w, _CONTENT_WIDTH - sev_w - v2_w - loc_w - where_w] \
            + ([where_w] if has_entries else [])
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(_TABLE_STYLE)
        # The table flows on straight after the summary and splits across
        # pages (header row repeated); the heading and notes stay with its
        # first rows rather than being left at the foot of a page.
        for flowable in story:
            flowable.keepWithNext = 1
        main_story.extend(story)
        main_story.append(t)
    doc.build(main_story)
    return buf.getvalue()
