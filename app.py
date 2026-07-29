import os
from datetime import date, timedelta
from flask import Flask, render_template, request, Response, abort
from fhir_client import FhirClient

app = Flask(__name__)
client = FhirClient()


def human_name(resource):
    names = resource.get("name", [])
    if not names:
        return resource.get("id", "unknown")
    n = names[0]
    given = " ".join(n.get("given", []))
    family = n.get("family", "")
    return f"{given} {family}".strip() or resource.get("id", "unknown")


def code_text(codeable_concept):
    """Best-effort human-readable text from a FHIR CodeableConcept."""
    if not codeable_concept:
        return "—"
    if codeable_concept.get("text"):
        return codeable_concept["text"]
    codings = codeable_concept.get("coding", [])
    if codings:
        return codings[0].get("display") or codings[0].get("code", "—")
    return "—"


def obs_value(obs):
    """Pull whichever value[x] field is populated on an Observation."""
    if "valueQuantity" in obs:
        q = obs["valueQuantity"]
        return f"{q.get('value', '')} {q.get('unit', '')}".strip()
    if "valueString" in obs:
        return obs["valueString"]
    if "valueCodeableConcept" in obs:
        return code_text(obs["valueCodeableConcept"])
    if "component" in obs:
        parts = []
        for c in obs["component"]:
            label = code_text(c.get("code"))
            val = ""
            if "valueQuantity" in c:
                q = c["valueQuantity"]
                val = f"{q.get('value', '')} {q.get('unit', '')}".strip()
            parts.append(f"{label}: {val}")
        return "; ".join(parts)
    return "—"


def specimen_collected(spec):
    return (spec.get("collection") or {}).get("collectedDateTime") or "—"


def specimen_received(spec):
    return spec.get("receivedTime") or "—"


app.jinja_env.filters["human_name"] = human_name
app.jinja_env.filters["code_text"] = code_text
app.jinja_env.filters["obs_value"] = obs_value
app.jinja_env.filters["specimen_collected"] = specimen_collected
app.jinja_env.filters["specimen_received"] = specimen_received


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", base_url=client.base_url)


@app.route("/search", methods=["GET"])
def search():
    name = request.args.get("name", "").strip()
    patient_id = request.args.get("patient_id", "").strip()
    nhs_number = request.args.get("nhs_number", "").strip()
    error = None
    patients = []
    try:
        if patient_id:
            patients = client.search_patients(patient_id=patient_id)
        elif nhs_number:
            patients = client.search_patients(nhs_number=nhs_number)
        elif name:
            patients = client.search_patients(name=name)
    except Exception as e:
        error = str(e)
    return render_template("index.html", base_url=client.base_url,
                            patients=patients, error=error,
                            searched_name=name, searched_id=patient_id,
                            searched_nhs=nhs_number)


@app.route("/patient/<patient_id>")
def patient_detail(patient_id):
    error = None
    orders, reports, report_obs, order_requester = [], [], {}, {}
    specimens_by_id = {}
    try:
        orders = client.lab_orders_for_patient(patient_id)
        for o in orders:
            order_requester[o["id"]] = client.requester_display(o)
            for spec in client.resolve_specimens(o):
                specimens_by_id[spec["id"]] = spec
        reports = client.lab_reports_for_patient(patient_id)
        for r in reports:
            report_obs[r["id"]] = client.observations_for_report(r)
            for spec in client.resolve_specimens(r):
                specimens_by_id[spec["id"]] = spec
    except Exception as e:
        error = str(e)
    specimens = list(specimens_by_id.values())
    order_chains = client.build_order_chains(orders)
    return render_template(
        "patient.html", patient_id=patient_id,
        orders=orders, order_chains=order_chains,
        reports=reports, report_obs=report_obs,
        order_requester=order_requester, specimens=specimens, error=error,
    )


def group_count(rows, key):
    """[{key: value, ...}, ...] -> [(value, count), ...] sorted by count desc."""
    counts = {}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


@app.route("/stats")
def stats():
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=6)).isoformat()

    error = None
    order_rows, report_rows = [], []
    try:
        orders = client.orders_in_range(start, end)
        order_rows = []
        for o in orders:
            patient = client.patient_for(o)
            order_rows.append({
                "date": (o.get("authoredOn") or "")[:10] or "Unknown",
                "organisation": client.order_organisation(o) or "Unknown",
                "indication": client.order_indication(o),
                "ics": client.patient_ics(patient) or "Unknown",
                "country": client.patient_country(patient) or "Unknown",
            })

        reports = client.reports_in_range(start, end)
        report_rows = []
        for r in reports:
            patient = client.patient_for(r)
            report_rows.append({
                "date": (r.get("issued") or r.get("effectiveDateTime") or "")[:10] or "Unknown",
                "organisation": client.report_organisation(r) or "Unknown",
                "indication": client.report_indication(r),
                "ics": client.patient_ics(patient) or "Unknown",
                "country": client.patient_country(patient) or "Unknown",
            })
    except Exception as e:
        error = str(e)

    return render_template(
        "stats.html", start=start, end=end, error=error,
        order_count=len(order_rows), report_count=len(report_rows),
        order_by_day=group_count(order_rows, "date"),
        order_by_org=group_count(order_rows, "organisation"),
        order_by_indication=group_count(order_rows, "indication"),
        order_by_ics=group_count(order_rows, "ics"),
        order_by_country=group_count(order_rows, "country"),
        report_by_day=group_count(report_rows, "date"),
        report_by_org=group_count(report_rows, "organisation"),
        report_by_indication=group_count(report_rows, "indication"),
        report_by_ics=group_count(report_rows, "ics"),
        report_by_country=group_count(report_rows, "country"),
    )


@app.route("/report/<report_id>/variants")
def report_variants(report_id):
    index = int(request.args.get("index", 0))
    try:
        report = client.get_report(report_id)
    except Exception as e:
        return f"Could not load report: {e}", 502

    attachment = client.get_presented_form(report, index)
    if attachment is None:
        abort(404, description="This report has no attached document at that index.")

    try:
        data, content_type = client.fetch_attachment_bytes(attachment)
    except Exception as e:
        return f"Could not fetch attachment: {e}", 502
    if data is None:
        abort(404, description="Attachment had neither inline data nor a URL.")
    if content_type and "pdf" not in content_type.lower():
        return (f"This attachment is '{content_type}', not a PDF — "
                f"variant/clinical-term extraction only works on PDF report documents."), 415

    try:
        text = client.extract_pdf_text(data)
    except Exception as e:
        return f"Could not extract text from PDF: {e}", 500

    variant_counts = client.extract_variant_types(text)

    clinical_terms, clinical_error = [], None
    try:
        clinical_terms = client.extract_clinical_terms(text)
    except ImportError:
        clinical_error = ("scispaCy isn't installed. Run "
                           "`pip install -r requirements.txt` (see README for the model download step).")
    except OSError:
        clinical_error = ("The scispaCy model 'en_core_sci_sm' isn't downloaded yet — see README for the install command.")
    except Exception as e:
        clinical_error = f"Clinical term extraction failed: {e}"

    return render_template(
        "variants.html", report_id=report_id,
        variant_counts=variant_counts,
        clinical_terms=clinical_terms, clinical_error=clinical_error,
        has_text=bool(text.strip()),
    )


@app.route("/report/<report_id>/pdf")
def report_pdf(report_id):
    index = int(request.args.get("index", 0))
    try:
        report = client.get_report(report_id)
    except Exception as e:
        return f"Could not load report: {e}", 502

    attachment = client.get_presented_form(report, index)
    if attachment is None:
        abort(404, description="This report has no attached document at that index.")

    try:
        data, content_type = client.fetch_attachment_bytes(attachment)
    except Exception as e:
        return f"Could not fetch attachment: {e}", 502
    if data is None:
        abort(404, description="Attachment had neither inline data nor a URL.")

    filename = attachment.get("title") or f"{report_id}-{index}.pdf"
    return Response(
        data, mimetype=content_type or "application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True)
