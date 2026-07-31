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


def code_value(codeable_concept):
    """First coding's raw `.code` from a FHIR CodeableConcept — ignores
    `.text`/`.display`, for when the code itself (not a human-readable
    label) is what's wanted, e.g. the ctDNA summary's Test column."""
    if not codeable_concept:
        return "—"
    codings = codeable_concept.get("coding", [])
    if codings:
        return codings[0].get("code") or "—"
    return "—"


def all_identifiers(resource):
    """Every `identifier` entry on a resource, formatted as "value
    (label)" strings — label is the system URI's last path segment (short
    and readable rather than the full URI), or just "value" if an entry
    has no system. Used where every identifier matters (e.g. the Cepheid
    Test Results screen), unlike the various single-system identifier
    lookups elsewhere (report_identifier(), specimen_identifier(), etc.)
    that pick out one specific one by a known system."""
    if not resource:
        return []
    entries = []
    for ident in resource.get("identifier", []):
        value = ident.get("value")
        if not value:
            continue
        system = ident.get("system")
        if system:
            label = system.rstrip("/").rsplit("/", 1)[-1]
            entries.append(f"{value} ({label})")
        else:
            entries.append(value)
    return entries


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


def observation_reference_range(component_or_obs):
    """Reference range text for an Observation or Observation.component —
    same "text, else low–high, else —" logic used inline elsewhere for
    top-level Observations, factored out so component_rows() can reuse it
    per component rather than per Observation."""
    ranges = component_or_obs.get("referenceRange")
    if not ranges:
        return "—"
    rr = ranges[0]
    if rr.get("text"):
        return rr["text"]
    if rr.get("low") and rr.get("high"):
        return f"{rr['low'].get('value', '')}–{rr['high'].get('value', '')}"
    return "—"


def component_rows(observations):
    """
    [{"label", "value", "reference_range", "flag"}, ...] flattened across
    every Observation.component in `observations` — one row per component,
    for a results table like the Cepheid BCR-ABL screen's (as opposed to
    obs_value()'s single joined-string summary of the same data). Reuses
    obs_value() on each component dict directly, since a component's
    value[x] fields are shaped the same as its parent Observation's.

    An Observation with no `component` array at all contributes no rows —
    this is specifically a components table, not a general Observation
    results table (see the existing per-Observation table on the patient
    page for that).
    """
    rows = []
    for obs in observations:
        for c in obs.get("component", []):
            rows.append({
                "label": code_text(c.get("code")),
                "value": obs_value(c),
                "reference_range": observation_reference_range(c),
                "flag": code_text(c["interpretation"][0]) if c.get("interpretation") else "—",
            })
    return rows


def observation_rows(observations):
    """
    [{"label", "value", "data_absent_reason"}, ...] — one row per top-level
    Observation, for the Cepheid screen's Observation-level results table
    (as opposed to component_rows()'s per-component breakdown of the same
    Observations). `value` is obs_value()'s usual value[x] summary;
    `data_absent_reason` is Observation.dataAbsentReason — populated
    instead of value[x] when a result couldn't be obtained (e.g.
    "not-asked"/"unknown"/"error" — see the FHIR data-absent-reason value
    set), so this is normally "—" whenever `value` isn't.
    """
    rows = []
    for obs in observations:
        rows.append({
            "label": code_text(obs.get("code")),
            "value": obs_value(obs),
            "data_absent_reason": code_text(obs["dataAbsentReason"]) if obs.get("dataAbsentReason") else "—",
        })
    return rows


def specimen_collected(spec):
    return (spec.get("collection") or {}).get("collectedDateTime") or "—"


def specimen_received(spec):
    return spec.get("receivedTime") or "—"


def reason_code_reference(order):
    """Raw code(s) behind ServiceRequest.reasonCode — e.g. a Genomic
    Clinical Indication reference number — ignoring display text (see
    code_value()); joined with "; " for multiple reasonCode entries."""
    codes = [code_value(rc) for rc in order.get("reasonCode", [])]
    codes = [c for c in codes if c and c != "—"]
    return "; ".join(codes) if codes else "—"


def conclusion_code_reference(report):
    """Raw code(s) behind DiagnosticReport.conclusionCode, each paired with
    a plain-language description (the coding's `.display`, falling back to
    the CodeableConcept's own `.text` — see code_text()) as "CODE -
    Description"; joined with "; " for multiple conclusionCode entries. An
    entry with no description available (or one identical to the code
    itself) shows just the code; an entry with no code at all is skipped."""
    entries = []
    for cc in report.get("conclusionCode", []):
        code = code_value(cc)
        if not code or code == "—":
            continue
        description = code_text(cc)
        if description and description not in ("—", code):
            entries.append(f"{code} - {description}")
        else:
            entries.append(code)
    return "; ".join(entries) if entries else "—"


app.jinja_env.filters["human_name"] = human_name
app.jinja_env.filters["code_text"] = code_text
app.jinja_env.filters["code_value"] = code_value
app.jinja_env.filters["test_directory_code"] = FhirClient.test_directory_code
app.jinja_env.filters["obs_value"] = obs_value
app.jinja_env.filters["specimen_collected"] = specimen_collected
app.jinja_env.filters["specimen_received"] = specimen_received
app.jinja_env.filters["specimen_identifier"] = FhirClient.specimen_identifier
app.jinja_env.filters["placer_identifier"] = FhirClient.placer_identifier
app.jinja_env.filters["filler_identifier"] = FhirClient.filler_identifier
app.jinja_env.filters["report_identifier"] = FhirClient.report_identifier
app.jinja_env.filters["reason_code_reference"] = reason_code_reference
app.jinja_env.filters["conclusion_code_reference"] = conclusion_code_reference


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
    patient = None
    orders, reports, report_obs, order_requester = [], [], {}, {}
    order_performer = {}
    report_interpreters = {}
    specimens_by_id = {}
    medical_record_numbers = []
    try:
        patient = client.get_patient(patient_id)
        medical_record_numbers = client.medical_record_numbers(patient)
        orders = client.lab_orders_for_patient(patient_id)
        for o in orders:
            order_requester[o["id"]] = client.requester_display(o)
            order_performer[o["id"]] = client.performer_display(o)
            for spec in client.resolve_specimens(o):
                specimens_by_id[spec["id"]] = spec
        reports = client.lab_reports_for_patient(patient_id)
        for r in reports:
            report_obs[r["id"]] = client.observations_for_report(r)
            report_interpreters[r["id"]] = client.results_interpreter_display(r)
            for spec in client.resolve_specimens(r):
                specimens_by_id[spec["id"]] = spec
    except Exception as e:
        error = str(e)
    specimens = list(specimens_by_id.values())
    order_chains = client.build_order_chains(orders)
    return render_template(
        "patient.html", patient_id=patient_id, patient=patient,
        nhs_number=FhirClient.nhs_number(patient),
        igene_patient_id=FhirClient.igene_patient_identifier(patient),
        medical_record_numbers=medical_record_numbers,
        general_practitioner=client.general_practitioner_display(patient),
        patient_ics=client.patient_ics_display(patient),
        orders=orders, order_chains=order_chains,
        reports=reports, report_obs=report_obs, report_interpreters=report_interpreters,
        order_requester=order_requester, order_performer=order_performer,
        specimens=specimens, error=error,
    )


@app.route("/patient/<patient_id>/clear-down", methods=["GET", "POST"])
def patient_clear_down(patient_id):
    """
    GET shows a confirmation page listing exactly what will be deleted;
    only POST actually deletes anything. Deliberately not a single bare
    "delete" link — this is an irreversible action against a live FHIR
    server, so it gets the same GET-confirms/POST-mutates split as any
    other destructive control, rather than firing on the first click.

    The confirm form's "also delete the Patient resource itself" checkbox
    is opt-in (unchecked by default) — ticking it switches the delete from
    clear_down_patient() (orders/reports/specimens only) to
    clear_down_patient_and_record() (same, plus the Patient resource),
    the same distinction the admin screen's bulk clear-down makes.
    """
    if request.method == "POST":
        error = None
        result = {"deleted": [], "failed": []}
        delete_patient_record = bool(request.form.get("delete_patient_record"))
        try:
            if delete_patient_record:
                result = client.clear_down_patient_and_record(patient_id)
            else:
                result = client.clear_down_patient(patient_id)
        except Exception as e:
            error = str(e)
        return render_template(
            "patient_clear_down_result.html", patient_id=patient_id,
            deleted=result["deleted"], failed=result["failed"], error=error,
        )

    error = None
    orders, reports, specimens = [], [], []
    try:
        orders = client.lab_orders_for_patient(patient_id)
        reports = client.lab_reports_for_patient(patient_id)
        specimens_by_id = {}
        for resource in orders + reports:
            for spec in client.resolve_specimens(resource):
                specimens_by_id[spec["id"]] = spec
        specimens = list(specimens_by_id.values())
    except Exception as e:
        error = str(e)
    return render_template(
        "patient_clear_down_confirm.html", patient_id=patient_id,
        orders=orders, reports=reports, specimens=specimens, error=error,
    )


@app.route("/admin")
def admin():
    """
    Admin screen: find test/synthetic patients by NHS number range, and
    orphaned (no-patient) ServiceRequests, each with its own destructive
    clear-down action below. This GET only searches/lists — nothing is
    deleted until one of the POST routes below runs.
    """
    error = None
    test_patients, orphaned_orders = [], []
    try:
        test_patients = [
            {
                "id": p.get("id"),
                "name": human_name(p),
                "nhs_number": FhirClient.nhs_number(p) or "—",
            }
            for p in client.patients_in_nhs_number_ranges()
        ]
        orphaned_orders = [
            {
                "id": o.get("id"),
                "test": FhirClient.test_directory_code(o.get("code")) or "—",
                "status": o.get("status") or "—",
                "authoredOn": o.get("authoredOn") or "—",
            }
            for o in client.orphaned_service_requests()
        ]
    except Exception as e:
        error = str(e)
    return render_template(
        "admin.html", test_patients=test_patients, orphaned_orders=orphaned_orders, error=error,
    )


@app.route("/admin/patients/confirm", methods=["POST"])
def admin_patients_confirm():
    """
    Confirmation page for the selected test patients: re-resolves each one
    and its order/report/specimen counts, so the final delete step (below)
    shows exactly what's about to be lost per patient rather than just an
    ID list. Nothing is deleted here — this is still a preview.
    """
    patient_ids = request.form.getlist("patient_id")
    error = None
    rows = []
    try:
        for pid in patient_ids:
            patient = client.get_patient(pid)
            orders = client.lab_orders_for_patient(pid)
            reports = client.lab_reports_for_patient(pid)
            specimens_by_id = {}
            for resource in orders + reports:
                for spec in client.resolve_specimens(resource):
                    specimens_by_id[spec["id"]] = spec
            rows.append({
                "id": pid,
                "name": human_name(patient) if patient else "Unknown",
                "nhs_number": FhirClient.nhs_number(patient) or "—",
                "order_count": len(orders),
                "report_count": len(reports),
                "specimen_count": len(specimens_by_id),
            })
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_patients_confirm.html", rows=rows, patient_ids=patient_ids, error=error,
    )


@app.route("/admin/patients/clear-down", methods=["POST"])
def admin_patients_clear_down():
    """Actually deletes the confirmed patients (Patient record + all their
    Specimens/DiagnosticReports/ServiceRequests) via
    clear_down_patient_and_record() — the only route in this pair that
    mutates anything."""
    patient_ids = request.form.getlist("patient_id")
    error = None
    deleted, failed = [], []
    try:
        for pid in patient_ids:
            result = client.clear_down_patient_and_record(pid)
            deleted.extend(result["deleted"])
            failed.extend(result["failed"])
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html", title="Patient clear-down result",
        deleted=deleted, failed=failed, error=error,
    )


@app.route("/admin/orphaned/clear-down", methods=["POST"])
def admin_orphaned_clear_down():
    """Deletes every orphaned (no-subject) ServiceRequest found on the
    admin screen. No separate confirm step — the admin screen's GET
    already lists every one of them in full before this button is
    reachable, unlike the per-patient action which re-confirms with
    per-patient counts."""
    error = None
    result = {"deleted": [], "failed": []}
    try:
        result = client.clear_down_orphaned_service_requests()
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html", title="Orphaned ServiceRequest clear-down result",
        deleted=result["deleted"], failed=result["failed"], error=error,
    )


def _order_worklist(fetch_orders):
    """Shared logic behind /work-orders and /test-orders: fetch a flat
    active-order list via `fetch_orders`, then build the per-order
    requester/performer/patient lookups and basedOn chain tree both screens
    render the same way (only the intent filter behind `fetch_orders` and
    the template differ). Each order_patient entry also carries
    `nhs_range_flag` (whether that patient's NHS number falls in
    FhirClient.NHS_NUMBER_TEST_RANGES) — computed here since patient_for()
    is already resolved once per order for the name lookup, so this costs
    no extra HTTP calls (resolve_reference() is cached); only test_orders.html
    currently renders it. order_performer is likewise built for both, though
    currently only work_orders.html renders it."""
    error = None
    orders, order_chains, order_requester, order_performer, order_patient = [], [], {}, {}, {}
    try:
        orders = fetch_orders()
        for o in orders:
            order_requester[o["id"]] = client.requester_display(o)
            order_performer[o["id"]] = client.performer_display(o)
            patient = client.patient_for(o)
            order_patient[o["id"]] = {
                "id": patient.get("id") if patient else None,
                "name": human_name(patient) if patient else "Unknown",
                "nhs_range_flag": bool(patient) and FhirClient.nhs_number_in_ranges(patient),
            }
        order_chains = client.build_order_chains(orders)
    except Exception as e:
        error = str(e)
    return orders, order_chains, order_requester, order_performer, order_patient, error


@app.route("/work-orders")
def work_orders():
    orders, order_chains, order_requester, order_performer, order_patient, error = _order_worklist(client.active_filler_orders)
    return render_template(
        "work_orders.html", orders=orders, order_chains=order_chains,
        order_requester=order_requester, order_performer=order_performer,
        order_patient=order_patient, error=error,
    )


@app.route("/test-orders")
def test_orders():
    orders, order_chains, order_requester, order_performer, order_patient, error = _order_worklist(client.active_placer_orders)
    unknown_patient_count = sum(1 for o in orders if order_patient.get(o["id"], {}).get("id") is None)
    return render_template(
        "test_orders.html", orders=orders, order_chains=order_chains,
        order_requester=order_requester, order_performer=order_performer,
        order_patient=order_patient,
        unknown_patient_count=unknown_patient_count, error=error,
    )


@app.route("/test-orders/clear-down-unknown-patient", methods=["POST"])
def test_orders_clear_down_unknown_patient():
    """
    Deletes every currently-active placer-order (intent=order/
    original-order) ServiceRequest whose patient can't be resolved
    (client.orders_with_unknown_patient()). Single POST, no separate
    confirm route — same reasoning as the admin screen's orphaned-
    ServiceRequest delete: no patient identity is involved (the whole
    point is the patient is unknown), and /test-orders's GET already shows
    "Unknown" against every one of these before this button is reachable.
    """
    error = None
    result = {"deleted": [], "failed": []}
    try:
        orders = client.active_placer_orders()
        result = client.clear_down_orders_with_unknown_patient(orders)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html", title="Unknown-patient test orders clear-down result",
        back_url="/test-orders", back_label="Back to test orders",
        deleted=result["deleted"], failed=result["failed"], error=error,
    )


def group_count(rows, key):
    """[{key: value, ...}, ...] -> [(value, count), ...] sorted by count desc."""
    counts = {}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def pivot_by_day(rows, key, top_n=10):
    """
    [{"date": ..., key: value, ...}, ...] -> {"days": [...], "columns": [...], "table": {day: {column: count}}}.

    A day-by-`key` cross-tab (e.g. day-by-organisation, day-by-indication) so
    trends over time are visible, rather than just a range-wide total per
    value. `columns` is capped at the `top_n` most frequent values overall
    (by total count desc); anything past that is folded into an "Other"
    column so a field with many distinct values (e.g. free-text indications)
    doesn't blow the table out sideways. `days` is chronological, with an
    "Unknown" bucket (missing/unparseable date) sorted last.
    """
    totals = {}
    for row in rows:
        totals[row[key]] = totals.get(row[key], 0) + 1
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    columns = [value for value, _ in ranked[:top_n]]
    column_set = set(columns)
    if len(ranked) > top_n:
        columns.append("Other")

    table = {}
    for row in rows:
        day, value = row["date"], row[key]
        column = value if value in column_set else "Other"
        table.setdefault(day, {})
        table[day][column] = table[day].get(column, 0) + 1

    days = sorted(table, key=lambda d: (d == "Unknown", d))
    return {"days": days, "columns": columns, "table": table}


def group_rows_by_organisation(rows):
    """ctdna_summary()'s flat, pre-sorted row list -> [(organisation,
    [row, ...]), ...], alphabetical with "Unknown" last; rows keep their
    incoming (Outstanding-first, most-recent-first) order within an
    organisation."""
    groups = {}
    for row in rows:
        groups.setdefault(row["organisation"], []).append(row)
    orgs = sorted(o for o in groups if o != "Unknown")
    if "Unknown" in groups:
        orgs.append("Unknown")
    return [(org, groups[org]) for org in orgs]


def group_clinical_terms_by_category(terms):
    """FhirClient.extract_clinical_terms()'s flat, count-sorted list ->
    [(category, [term, ...]), ...], categories alphabetical with "Unlinked"
    always last; terms keep their incoming (count desc) order within a
    category."""
    groups = {}
    for term in terms:
        groups.setdefault(term["category"], []).append(term)
    categories = sorted(c for c in groups if c != "Unlinked")
    if "Unlinked" in groups:
        categories.append("Unlinked")
    return [(category, groups[category]) for category in categories]


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
        order_pivot_org=pivot_by_day(order_rows, "organisation"),
        order_pivot_indication=pivot_by_day(order_rows, "indication"),
        report_pivot_org=pivot_by_day(report_rows, "organisation"),
        report_pivot_indication=pivot_by_day(report_rows, "indication"),
    )


@app.route("/ctdna")
def ctdna_summary():
    error = None
    rows = []
    try:
        orders, reports_by_order = client.ctdna_orders()
        cutoff = (date.today() - timedelta(days=30)).isoformat()

        for order in orders:
            order_id = order.get("id")
            # "Outstanding" = any status other than "completed" (draft,
            # active, on-hold, revoked, ...) — shown regardless of age.
            # "Completed" orders are bounded to the last month below.
            is_completed = order.get("status") == "completed"
            report = reports_by_order.get(order_id)
            reported_date = None
            if report:
                reported_date = (report.get("issued") or report.get("effectiveDateTime") or "")[:10] or None

            if is_completed:
                # Bound "completed" by when it was reported (the actual
                # completion event), falling back to order date if no
                # report resolved.
                completion_date = reported_date or (order.get("authoredOn") or "")[:10]
                if not completion_date or completion_date < cutoff:
                    continue

            specimens = client.resolve_specimens(order)
            if not specimens and report:
                specimens = client.resolve_specimens(report)
            specimen = specimens[0] if specimens else None
            patient = client.patient_for(order)
            conclusion = "; ".join(
                code_text(cc) for cc in (report.get("conclusionCode") or [])
            ) if report else ""

            org_resource = client.order_organisation_resource(order)
            org_name = (org_resource.get("name") if org_resource else None) or client.order_organisation(order) or "Unknown"
            ods_code = client.order_organisation_ods(order)
            organisation = f"{org_name} ({ods_code})" if ods_code else org_name

            rows.append({
                "order_id": order_id,
                "organisation": organisation,
                "patient_id": patient.get("id") if patient else None,
                "patient_name": human_name(patient) if patient else "Unknown",
                "test": FhirClient.test_directory_code(order.get("code")) or "—",
                "status": "Completed" if is_completed else "Outstanding",
                "order_date_raw": order.get("authoredOn") or "",
                "order_date": order.get("authoredOn") or "—",
                "collected_date": specimen_collected(specimen) if specimen else "—",
                "received_date": specimen_received(specimen) if specimen else "—",
                "reported_date": reported_date or "—",
                "placer_id": FhirClient.placer_identifier(order) or "—",
                "igene_id": client.igene_report_identifier(order, report) or "—",
                "conclusion": conclusion or "—",
            })

        # Most recently ordered first within each group; Outstanding above
        # Completed (two stable sorts so both orderings hold at once).
        rows.sort(key=lambda r: r["order_date_raw"], reverse=True)
        rows.sort(key=lambda r: r["status"] != "Outstanding")
        rows_by_org = group_rows_by_organisation(rows)
    except Exception as e:
        error = str(e)
        rows_by_org = []

    return render_template("ctdna.html", rows_by_org=rows_by_org, error=error)


@app.route("/cepheid-results")
def cepheid_results():
    """
    Cepheid Test Results: DiagnosticReports with a BCRABL code
    (client.bcrabl_reports()), each shown with its originating order,
    specimen, an Observation-level results table (observation_rows() — each
    linked Observation's top-level `value[x]` and `dataAbsentReason`), and a
    component-level results table built from every linked Observation's
    `.component` entries (component_rows()).
    """
    error = None
    rows = []
    no_identifier_count = 0
    duplicate_count = 0
    try:
        reports = client.bcrabl_reports()
        no_identifier_count = len(client.bcrabl_reports_without_identifiers(reports))
        duplicate_count = len(client.duplicate_bcrabl_reports(reports))
        for report in reports:
            order = client.order_for_report(report)
            patient = client.patient_for(report) or (client.patient_for(order) if order else None)

            specimens = client.resolve_specimens(report)
            if not specimens and order:
                specimens = client.resolve_specimens(order)
            specimen = specimens[0] if specimens else None

            observations = client.observations_for_report(report)

            rows.append({
                "report_id": report.get("id"),
                "test": code_text(report.get("code")),
                "status": report.get("status") or "—",
                "date_reported_raw": report.get("issued") or report.get("effectiveDateTime") or "",
                "date_reported": report.get("issued") or report.get("effectiveDateTime") or "—",
                "last_updated": (report.get("meta") or {}).get("lastUpdated") or "—",
                "identifiers": all_identifiers(report),
                "patient_id": patient.get("id") if patient else None,
                "patient_name": human_name(patient) if patient else "Unknown",
                "order_id": order.get("id") if order else "—",
                "order_date": (order.get("authoredOn") if order else None) or "—",
                "order_status": (order.get("status") if order else None) or "—",
                "specimen_type": code_text(specimen.get("type")) if specimen else "—",
                "collected_date": specimen_collected(specimen) if specimen else "—",
                "received_date": specimen_received(specimen) if specimen else "—",
                "specimen_id": (FhirClient.specimen_identifier(specimen) if specimen else None) or "—",
                "observations": observation_rows(observations),
                "components": component_rows(observations),
            })
        rows.sort(key=lambda r: r["date_reported_raw"], reverse=True)
    except Exception as e:
        error = str(e)

    no_component_results_count = sum(1 for r in rows if not r["components"])
    return render_template(
        "cepheid_results.html", rows=rows,
        no_component_results_count=no_component_results_count,
        no_identifier_count=no_identifier_count,
        duplicate_count=duplicate_count,
        error=error,
    )


@app.route("/cepheid-results/clear-down-no-components", methods=["POST"])
def cepheid_results_clear_down_no_components():
    """
    Deletes every currently-listed BCRABL DiagnosticReport with no
    component-level results at all
    (client.clear_down_bcrabl_reports_without_components()). Single POST,
    no separate confirm route — same reasoning as the admin screen's
    orphaned-ServiceRequest delete and the test orders unknown-patient
    delete: this is a mechanical, well-defined criterion (no component
    data to show), not tied to a specific identifiable patient, and
    /cepheid-results's GET already shows "No component-level results
    found on this report's Observations" against every one of these
    before this button is reachable.
    """
    error = None
    result = {"deleted": [], "failed": []}
    try:
        reports = client.bcrabl_reports()
        result = client.clear_down_bcrabl_reports_without_components(reports)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html",
        title="BCRABL reports without component results — clear-down result",
        back_url="/cepheid-results", back_label="Back to Cepheid Test Results",
        deleted=result["deleted"], failed=result["failed"], error=error,
    )


@app.route("/cepheid-results/clear-down-no-identifiers", methods=["POST"])
def cepheid_results_clear_down_no_identifiers():
    """
    Deletes every currently-listed BCRABL DiagnosticReport with no
    identifier at all, plus its associated Specimen
    (client.clear_down_bcrabl_reports_without_identifiers()) — a specimen
    is only deleted if no *other* BCRABL report still references it.
    Single POST, no separate confirm route — same reasoning as the other
    mechanical clear-downs on this screen.
    """
    error = None
    result = {"deleted": [], "failed": []}
    try:
        reports = client.bcrabl_reports()
        result = client.clear_down_bcrabl_reports_without_identifiers(reports)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html",
        title="BCRABL reports without identifiers — clear-down result",
        back_url="/cepheid-results", back_label="Back to Cepheid Test Results",
        deleted=result["deleted"], failed=result["failed"], error=error,
    )


@app.route("/cepheid-results/clear-down-duplicates", methods=["POST"])
def cepheid_results_clear_down_duplicates():
    """
    Deletes duplicate BCRABL DiagnosticReports — ones sharing an identical
    identifier with another report in the list — keeping the
    most-recently-updated report in each duplicate group
    (client.clear_down_duplicate_bcrabl_reports()). Single POST, no
    separate confirm route — same reasoning as the other mechanical
    clear-downs on this screen.
    """
    error = None
    result = {"deleted": [], "failed": []}
    try:
        reports = client.bcrabl_reports()
        result = client.clear_down_duplicate_bcrabl_reports(reports)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html",
        title="Duplicate BCRABL reports — clear-down result",
        back_url="/cepheid-results", back_label="Back to Cepheid Test Results",
        deleted=result["deleted"], failed=result["failed"], error=error,
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

    clinical_by_category, clinical_error = [], None
    try:
        clinical_terms = client.extract_clinical_terms(text)
        clinical_by_category = group_clinical_terms_by_category(clinical_terms)
    except ImportError:
        clinical_error = ("scispaCy isn't installed. Run "
                           "`pip install -r requirements.txt` (see README for the model download step).")
    except OSError:
        clinical_error = ("The scispaCy model 'en_core_sci_sm' or UMLS knowledge base isn't downloaded "
                           "yet — see README for the install/first-run details.")
    except Exception as e:
        clinical_error = f"Clinical term extraction failed: {e}"

    return render_template(
        "variants.html", report_id=report_id,
        variant_counts=variant_counts,
        clinical_by_category=clinical_by_category, clinical_error=clinical_error,
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
