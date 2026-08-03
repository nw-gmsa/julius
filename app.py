import os
import re
import secrets
import difflib
from datetime import date, timedelta
from flask import Flask, render_template, request, Response, abort, redirect, session, g, url_for
from werkzeug.local import LocalProxy
import requests
import pandas as pd
import plotly.express as px
from fhir_client import FhirClient

app = Flask(__name__)
# Falls back to a random key if SECRET_KEY isn't set, which works fine for
# a single long-lived process (see docs/windows-iis-deployment.md) but
# invalidates everyone's session on restart — set SECRET_KEY in production
# if that's not acceptable.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Login exchanges a username/password for a FhirClient built from those
# credentials (instead of the old single FHIR_USER/FHIR_PASSWORD env-var
# client shared by everyone). The client itself — and so the password —
# stays server-side in this dict, keyed by a random token; the browser's
# session cookie only ever holds that token, never the credentials. No
# expiry beyond an explicit /logout: fine for a small internal app on one
# long-lived process, but a lot of abandoned logins would leak memory.
_session_clients = {}

LOGIN_EXEMPT_ENDPOINTS = {"login", "static"}


@app.before_request
def _load_client():
    if request.endpoint in LOGIN_EXEMPT_ENDPOINTS:
        return
    fhir_client = _session_clients.get(session.get("sid"))
    if fhir_client is None:
        return redirect(url_for("login", next=request.path))
    g.client = fhir_client


# Existing routes/helpers below were all written against a module-level
# `client` — this proxy resolves to the logged-in user's FhirClient
# (set on `g` by _load_client above) so none of them needed to change.
client = LocalProxy(lambda: g.client)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    next_url = request.values.get("next") or url_for("index")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("index")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        try:
            candidate = FhirClient(user=username, password=password)
            candidate.verify_credentials()
        except ValueError as e:
            error = str(e)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (401, 403):
                error = "Incorrect username or password."
            else:
                error = f"FHIR server error: {e}"
        except requests.RequestException as e:
            error = f"Could not reach FHIR server: {e}"
        else:
            sid = secrets.token_urlsafe(32)
            _session_clients[sid] = candidate
            session.clear()
            session["sid"] = sid
            session["username"] = username
            return redirect(next_url)

    return render_template("login.html", error=error, next=next_url)


@app.route("/logout")
def logout():
    sid = session.pop("sid", None)
    _session_clients.pop(sid, None)
    session.clear()
    return redirect(url_for("login"))


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
    order_number = request.args.get("order_number", "").strip()
    error = None
    patients = []
    identifier_matches = None
    try:
        if order_number:
            identifier_matches, single_patient_id = _find_by_order_or_report_number(order_number)
            if single_patient_id:
                return redirect(f"/patient/{single_patient_id}")
        elif patient_id:
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
                            searched_nhs=nhs_number, searched_order_number=order_number,
                            identifier_matches=identifier_matches)


def _find_by_order_or_report_number(value):
    """
    Look up `value` as an order/test number against both ServiceRequest and
    DiagnosticReport identifiers (find_orders_by_identifier()/
    find_reports_by_identifier()) — the caller doesn't know in advance
    whether it's an order (placer/filler) number or a report (e.g. iGene)
    number, so both are searched and whatever matches is shown.

    Returns (matches, single_patient_id):
    - `matches` is a display-ready list of {"kind", "resource_id", "test",
      "patient_id", "patient_name"} dicts, one per matching order/report,
      for the search screen's disambiguation table.
    - `single_patient_id` is that one patient's id if every match resolves
      to the *same* patient (the common case — an order/report number
      belongs to one patient), so the caller can redirect straight to the
      patient page; None if there were zero matches, unresolvable
      patients, or matches spanning more than one patient (left for the
      user to disambiguate from the table instead of guessing).
    """
    matches = []
    patient_ids = set()
    for resource, kind in (
        [(o, "Order") for o in client.find_orders_by_identifier(value)]
        + [(r, "Report") for r in client.find_reports_by_identifier(value)]
    ):
        patient = client.patient_for(resource)
        matches.append({
            "kind": kind,
            "resource_id": resource.get("id"),
            "test": code_text(resource.get("code")),
            "patient_id": patient.get("id") if patient else None,
            "patient_name": human_name(patient) if patient else "Unknown",
        })
        if patient and patient.get("id"):
            patient_ids.add(patient["id"])

    single_patient_id = next(iter(patient_ids)) if len(patient_ids) == 1 else None
    return matches, single_patient_id


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

    order_organisation = {o["id"]: (client.order_organisation(o) or "Unknown") for o in orders}
    order_test = {o["id"]: (FhirClient.test_directory_code(o.get("code")) or "—") for o in orders}
    organisations = sorted(set(order_organisation.values()))
    tests = sorted(set(order_test.values()))

    selected_org = request.args.get("org", "")
    selected_test = request.args.get("test", "")
    sort = request.args.get("sort", "")

    filtered_orders = _filter_orders_by_org_and_test(orders, order_organisation, order_test, selected_org, selected_test)
    order_chains = client.build_order_chains(filtered_orders)
    if sort in ("ordered_asc", "ordered_desc"):
        _sort_order_chains(order_chains, reverse=(sort == "ordered_desc"))

    return render_template(
        "work_orders.html", orders=filtered_orders, order_chains=order_chains,
        order_requester=order_requester, order_performer=order_performer,
        order_patient=order_patient,
        organisations=organisations, tests=tests,
        selected_org=selected_org, selected_test=selected_test, sort=sort,
        error=error,
    )


def _sort_order_chains(nodes, reverse=False):
    """Sort build_order_chains()'s node list by the node's own order's
    authoredOn, in place, recursing into each node's children so a
    reanalysis/cascade child is sorted the same way among its siblings —
    not just the root level. Orders with no authoredOn sort first
    ascending / last descending."""
    nodes.sort(key=lambda n: n["order"].get("authoredOn") or "", reverse=reverse)
    for n in nodes:
        _sort_order_chains(n["children"], reverse=reverse)


def _filter_orders_by_org_and_test(orders, order_organisation, order_test, selected_org, selected_test):
    filtered = orders
    if selected_org:
        filtered = [o for o in filtered if order_organisation.get(o["id"]) == selected_org]
    if selected_test:
        filtered = [o for o in filtered if order_test.get(o["id"]) == selected_test]
    return filtered


@app.route("/test-orders")
def test_orders():
    orders, order_chains, order_requester, order_performer, order_patient, error = _order_worklist(client.active_placer_orders)

    order_organisation = {o["id"]: (client.order_organisation(o) or "Unknown") for o in orders}
    order_test = {o["id"]: (FhirClient.test_directory_code(o.get("code")) or "—") for o in orders}
    organisations = sorted(set(order_organisation.values()))
    tests = sorted(set(order_test.values()))

    selected_org = request.args.get("org", "")
    selected_test = request.args.get("test", "")
    sort = request.args.get("sort", "")

    filtered_orders = _filter_orders_by_org_and_test(orders, order_organisation, order_test, selected_org, selected_test)
    order_chains = client.build_order_chains(filtered_orders)
    if sort in ("ordered_asc", "ordered_desc"):
        _sort_order_chains(order_chains, reverse=(sort == "ordered_desc"))

    unknown_patient_count = sum(1 for o in filtered_orders if order_patient.get(o["id"], {}).get("id") is None)
    return render_template(
        "test_orders.html", orders=filtered_orders, order_chains=order_chains,
        order_requester=order_requester, order_performer=order_performer,
        order_patient=order_patient,
        organisations=organisations, tests=tests,
        selected_org=selected_org, selected_test=selected_test, sort=sort,
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

    Scoped to the same org/test filter the page was showing when the
    button was clicked (passed through as hidden fields) — otherwise the
    displayed "N of the orders above" count could disagree with how many
    this actually deletes.
    """
    selected_org = request.form.get("org", "")
    selected_test = request.form.get("test", "")
    error = None
    result = {"deleted": [], "failed": []}
    try:
        orders = client.active_placer_orders()
        order_organisation = {o["id"]: (client.order_organisation(o) or "Unknown") for o in orders}
        order_test = {o["id"]: (FhirClient.test_directory_code(o.get("code")) or "—") for o in orders}
        orders = _filter_orders_by_org_and_test(orders, order_organisation, order_test, selected_org, selected_test)
        result = client.clear_down_orders_with_unknown_patient(orders)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html", title="Unknown-patient test orders clear-down result",
        back_url=url_for("test_orders", org=selected_org, test=selected_test), back_label="Back to test orders",
        deleted=result["deleted"], failed=result["failed"], error=error,
    )


def group_count(rows, key):
    """[{key: value, ...}, ...] -> [(value, count), ...] sorted by count desc."""
    counts = {}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def orders_by_organisation_geocoded(orders):
    """
    Order counts per requesting organisation, geocoded for the /stats map —
    [{"name", "ods", "lat", "lon", "count"}, ...], one entry per distinct
    Organization resource resolved from ServiceRequest.requester
    (order_organisation_resource()/order_organisation_ods(), same
    resolution chain as the ctDNA summary's organisation column). Grouped
    by the Organization's own `id` where it resolved (falling back to its
    display name for an order whose requester didn't resolve to a full
    resource, so it's still counted even though it can't be placed on the
    map) rather than by display string, since that's a stable identity to
    geocode once per organisation regardless of how many orders it has.

    Only entries with both `lat` and `lon` (i.e. an address with a postcode
    that geocode_postcode() could resolve) are meant to be plotted; entries
    without are still returned so the caller can report how many
    organisations/orders couldn't be placed, rather than that count being
    silently dropped.

    Takes a plain list of ServiceRequest resources, so the "Reports by
    ordering provider" map reuses this unchanged — the caller passes each
    report's originating order (client.order_for_report(r)) instead of the
    order itself, and counts come out per-report rather than per-order.
    """
    orgs = {}
    for order in orders:
        org_resource = client.order_organisation_resource(order)
        name = (org_resource.get("name") if org_resource else None) or client.order_organisation(order) or "Unknown"
        key = (org_resource.get("id") if org_resource else None) or name
        if key not in orgs:
            geocode = client.organisation_geocode(org_resource) if org_resource else None
            orgs[key] = {
                "name": name,
                "ods": client.organisation_ods_code(org_resource) if org_resource else None,
                "lat": geocode[0] if geocode else None,
                "lon": geocode[1] if geocode else None,
                "count": 0,
            }
        orgs[key]["count"] += 1
    return list(orgs.values())


def _normalize_icb_name(name):
    """
    Normalise an ICS display name for fuzzy-matching against the ONS
    ICB23NM field. The /stats "by requesting organisation's ICS" maps feed
    this from FhirClient.organisation_ics() (a point-in-polygon lookup
    against the same ONS boundary dataset), which already returns the
    official ICB23NM string verbatim — so those rows normally hit the exact
    match below. This normalisation/fuzzy-match pair mainly exists as a
    safety net for any other ICS-name source that isn't guaranteed to match
    ONS's wording verbatim (e.g. this server might say "NHS Greater
    Manchester ICB" where ONS says "NHS Greater Manchester Integrated Care
    Board", or use "&" where ONS spells out "and" — many of the 42 official
    names are "X and Y" or "X, Y and Z" compounds, so that one substitution
    alone accounts for a lot of otherwise-missed matches). Stripping a
    leading "NHS", a trailing "Integrated Care Board"/"ICB", normalising
    "&" to "and", and dropping all remaining non-alphanumeric characters
    down to a lowercase core (e.g. "greatermanchester") gives both sides a
    fair shot at an exact match; ics_choropleth_html() falls back further
    to a similarity-ratio best match on this normalised form (see
    _best_icb_match()) if that still misses — needed because a plain
    substring check fails whenever a filler word like "the" is
    inserted/dropped in the middle of a name (e.g. "Cornwall and Isles of
    Scilly" vs the official "...and the Isles of Scilly" — neither is a
    contiguous substring of the other).
    """
    if not name:
        return ""
    n = re.sub(r"(?i)^nhs\s+", "", name.strip())
    n = re.sub(r"(?i)\s+integrated care board$", "", n)
    n = re.sub(r"(?i)\s+icb$", "", n)
    n = n.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", n.lower())


#: Minimum difflib.SequenceMatcher ratio for _best_icb_match() to accept a
#: fuzzy match rather than leave an ICS name unmatched. Calibrated so
#: genuinely-the-same name with minor wording differences ("&" vs "and", a
#: dropped "the", missing "NHS"/"ICB") scores at or near 1.0, while an
#: unrelated org name (a hospital trust, "Unknown", etc.) scores well under
#: 0.5 — even the closest look-alike pairs in the dataset (the five
#: "North/South/East/West/Central London" ICBs, which differ by one
#: directional word) still separate cleanly at this threshold.
ICB_FUZZY_MATCH_THRESHOLD = 0.82


def _best_icb_match(norm, icb_norms):
    """
    Best-scoring ICB match for a normalised ICS name against every
    normalised ICB name, via difflib's SequenceMatcher ratio — more
    forgiving than plain substring containment, which requires one string
    to appear as an unbroken run inside the other and so misses anything
    with a word inserted/dropped/reordered in the middle. Returns the
    best-matching normalised ICB name, or None if nothing clears
    ICB_FUZZY_MATCH_THRESHOLD.
    """
    if not norm:
        return None
    best_norm, best_ratio = None, 0.0
    for icb_norm in icb_norms:
        ratio = difflib.SequenceMatcher(None, norm, icb_norm).ratio()
        if ratio > best_ratio:
            best_norm, best_ratio = icb_norm, ratio
    return best_norm if best_ratio >= ICB_FUZZY_MATCH_THRESHOLD else None


def ics_choropleth_html(ics_counts):
    """
    [(ics_name, count), ...] (from group_count(order_rows, "ics")) -> a
    self-contained Plotly Express choropleth HTML fragment shading each
    matched NHS Integrated Care Board by order count, using
    FhirClient.fetch_icb_boundaries() for the boundary polygons and
    _normalize_icb_name() to match them against our resolved ICS names.

    Every ICB in the boundary dataset is included as a row (unmatched ones
    at count=0), not just the ones with orders/reports — so every ICB's
    outline is drawn (they tile to the England outline), giving the map
    geographic context beyond just the handful of regions with data.
    `update_geos()` also turns on a UK/Europe basemap (coastline, country
    borders) underneath, further placing the ICBs within the UK outline.

    Returns (html, unmatched_count, unmatched_names): `html` is None if
    boundary data couldn't be fetched or nothing in `ics_counts` matched a
    boundary at all; `unmatched_count` is the order count across every ICS
    name that didn't match any boundary, and `unmatched_names` lists which
    ICS names those were (so the caller can surface both — same convention
    as order_map_unmapped_count for the organisation map, but naming the
    actual strings too makes a real mismatch immediately diagnosable
    instead of just "something didn't match").
    """
    boundaries = FhirClient.fetch_icb_boundaries()
    total = sum(count for _, count in ics_counts)
    if not boundaries:
        return None, total, [name for name, _ in ics_counts]

    icb_by_norm = {}
    icb_name_by_code = {}
    for feature in boundaries["features"]:
        code = feature["properties"]["ICB23CD"]
        icb_by_norm[_normalize_icb_name(feature["properties"]["ICB23NM"])] = code
        icb_name_by_code[code] = feature["properties"]["ICB23NM"]

    counts_by_code = {}
    unmatched = 0
    unmatched_names = []
    for ics_name, count in ics_counts:
        norm = _normalize_icb_name(ics_name)
        code = icb_by_norm.get(norm)
        if not code:
            best_norm = _best_icb_match(norm, icb_by_norm.keys())
            code = icb_by_norm.get(best_norm)
        if code:
            counts_by_code[code] = counts_by_code.get(code, 0) + count
        else:
            unmatched += count
            unmatched_names.append(ics_name)

    if not counts_by_code:
        return None, unmatched, unmatched_names

    rows = [
        {"icb_code": code, "ics_name": name, "count": counts_by_code.get(code, 0)}
        for code, name in icb_name_by_code.items()
    ]
    fig = px.choropleth(
        pd.DataFrame(rows), geojson=boundaries, locations="icb_code",
        featureidkey="properties.ICB23CD", color="count",
        hover_name="ics_name", hover_data={"icb_code": False, "count": True},
        color_continuous_scale="Blues", labels={"count": "Orders"},
    )
    fig.update_traces(marker_line_color="#666666", marker_line_width=0.6)
    fig.update_geos(
        fitbounds="locations", visible=True,
        showcountries=True, countrycolor="#666666",
        showcoastlines=True, coastlinecolor="#666666",
        showsubunits=True, subunitcolor="#999999",
        showland=True, landcolor="#f2f2f2",
        showocean=True, oceancolor="#eaf3fa",
        resolution=50,
    )
    fig.update_layout(margin={"r": 0, "t": 0, "l": 0, "b": 0}, height=520)
    return fig.to_html(full_html=False, include_plotlyjs="cdn"), unmatched, unmatched_names


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


@app.route("/stats")
def stats():
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=6)).isoformat()

    error = None
    order_rows, report_rows = [], []
    order_map_points, order_map_unmapped_count = [], 0
    order_ics_map_html, order_ics_map_unmatched_count, order_ics_map_unmatched_names = None, 0, []
    report_map_points, report_map_unmapped_count = [], 0
    report_ics_map_html, report_ics_map_unmatched_count, report_ics_map_unmatched_names = None, 0, []
    try:
        orders = client.orders_in_range(start, end)
        order_rows = []
        for o in orders:
            patient = client.patient_for(o)
            order_org_resource = client.order_organisation_resource(o)
            order_rows.append({
                "date": (o.get("authoredOn") or "")[:10] or "Unknown",
                "organisation": client.order_organisation(o) or "Unknown",
                "indication": client.order_indication(o),
                "ics": client.organisation_ics(order_org_resource) or "Unknown",
                "country": client.patient_country(patient) or "Unknown",
            })

        org_geo = orders_by_organisation_geocoded(orders)
        order_map_points = [g for g in org_geo if g["lat"] is not None and g["lon"] is not None]
        order_map_unmapped_count = sum(g["count"] for g in org_geo if g["lat"] is None)

        order_ics_map_html, order_ics_map_unmatched_count, order_ics_map_unmatched_names = ics_choropleth_html(group_count(order_rows, "ics"))

        reports = client.reports_in_range(start, end)
        report_rows = []
        report_orders = []
        for r in reports:
            patient = client.patient_for(r)
            # order_for_report()'s originating ServiceRequest — separate from
            # report_organisation() (the *performing* lab, from
            # DiagnosticReport.performer): this is who *ordered* the test,
            # i.e. the same "requesting organisation" concept the orders
            # side already maps, just resolved via the report's `basedOn`.
            ordering_order = client.order_for_report(r)
            ordering_org_resource = None
            if ordering_order:
                report_orders.append(ordering_order)
                ordering_org_resource = client.order_organisation_resource(ordering_order)
            report_rows.append({
                "date": (r.get("issued") or r.get("effectiveDateTime") or "")[:10] or "Unknown",
                "organisation": client.report_organisation(r) or "Unknown",
                "ordering_provider": (client.order_organisation(ordering_order) if ordering_order else None) or "Unknown",
                "indication": client.report_indication(r),
                "ics": client.organisation_ics(ordering_org_resource) or "Unknown",
                "country": client.patient_country(patient) or "Unknown",
            })

        report_org_geo = orders_by_organisation_geocoded(report_orders)
        report_map_points = [g for g in report_org_geo if g["lat"] is not None and g["lon"] is not None]
        report_map_unmapped_count = sum(g["count"] for g in report_org_geo if g["lat"] is None)

        report_ics_map_html, report_ics_map_unmatched_count, report_ics_map_unmatched_names = ics_choropleth_html(group_count(report_rows, "ics"))
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
        order_map_points=order_map_points,
        order_map_unmapped_count=order_map_unmapped_count,
        order_ics_map_html=order_ics_map_html,
        order_ics_map_unmatched_count=order_ics_map_unmatched_count,
        order_ics_map_unmatched_names=order_ics_map_unmatched_names,
        report_by_day=group_count(report_rows, "date"),
        report_by_org=group_count(report_rows, "organisation"),
        report_by_ordering_provider=group_count(report_rows, "ordering_provider"),
        report_by_indication=group_count(report_rows, "indication"),
        report_by_ics=group_count(report_rows, "ics"),
        report_by_country=group_count(report_rows, "country"),
        report_map_points=report_map_points,
        report_map_unmapped_count=report_map_unmapped_count,
        report_ics_map_html=report_ics_map_html,
        report_ics_map_unmatched_count=report_ics_map_unmatched_count,
        report_ics_map_unmatched_names=report_ics_map_unmatched_names,
        order_pivot_org=pivot_by_day(order_rows, "organisation"),
        order_pivot_indication=pivot_by_day(order_rows, "indication"),
        report_pivot_org=pivot_by_day(report_rows, "organisation"),
        report_pivot_indication=pivot_by_day(report_rows, "indication"),
    )


@app.route("/ctdna")
def ctdna_summary():
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=30)).isoformat()

    error = None
    rows = []
    try:
        orders, reports_by_order = client.ctdna_orders(start=start, end=end)

        for order in orders:
            order_id = order.get("id")
            # "Outstanding" = any status other than "completed" (draft,
            # active, on-hold, revoked, ...). client.ctdna_orders() already
            # bounds the outstanding fetch itself to [start, end] by
            # authored date (to avoid a 413 on servers with a large
            # backlog), so nothing further to filter here. "Completed"
            # orders are bounded to [start, end] below, by completion
            # (report-issued) date rather than authored date.
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
                if not completion_date or completion_date < start or completion_date > end:
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

    return render_template("ctdna.html", rows_by_org=rows_by_org, error=error, start=start, end=end)


@app.route("/cepheid-results")
def cepheid_results():
    """
    Cepheid Test Results: DiagnosticReports with a BCRABL code
    (client.bcrabl_reports()), each shown with its originating order,
    specimen, an Observation-level results table (observation_rows() — each
    linked Observation's top-level `value[x]` and `dataAbsentReason`), and a
    component-level results table built from every linked Observation's
    `.component` entries (component_rows()).

    Bounded to DiagnosticReport.date within [start, end] (query params,
    same convention as /stats), defaulting to a rolling last-30-days
    window rather than the unbounded query bcrabl_reports() still supports
    if called with no dates.
    """
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=30)).isoformat()

    error = None
    rows = []
    no_identifier_count = 0
    duplicate_count = 0
    try:
        reports = client.bcrabl_reports(start, end)
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
        error=error, start=start, end=end,
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
    start = request.form.get("start")
    end = request.form.get("end")
    error = None
    result = {"deleted": [], "failed": []}
    try:
        reports = client.bcrabl_reports(start, end)
        result = client.clear_down_bcrabl_reports_without_components(reports)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html",
        title="BCRABL reports without component results — clear-down result",
        back_url=url_for("cepheid_results", start=start, end=end), back_label="Back to Cepheid Test Results",
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
    start = request.form.get("start")
    end = request.form.get("end")
    error = None
    result = {"deleted": [], "failed": []}
    try:
        reports = client.bcrabl_reports(start, end)
        result = client.clear_down_bcrabl_reports_without_identifiers(reports)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html",
        title="BCRABL reports without identifiers — clear-down result",
        back_url=url_for("cepheid_results", start=start, end=end), back_label="Back to Cepheid Test Results",
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
    start = request.form.get("start")
    end = request.form.get("end")
    error = None
    result = {"deleted": [], "failed": []}
    try:
        reports = client.bcrabl_reports(start, end)
        result = client.clear_down_duplicate_bcrabl_reports(reports)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html",
        title="Duplicate BCRABL reports — clear-down result",
        back_url=url_for("cepheid_results", start=start, end=end), back_label="Back to Cepheid Test Results",
        deleted=result["deleted"], failed=result["failed"], error=error,
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
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
