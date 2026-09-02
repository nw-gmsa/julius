import os
import re
import json
import secrets
import difflib
import threading
from datetime import date, timedelta
from flask import Flask, render_template, request, Response, abort, redirect, session, g, url_for
from werkzeug.local import LocalProxy
import requests
import pandas as pd
import plotly.express as px
from fhir_client import FhirClient
from iris_client import IrisClient
from pdf_report import quality_report_pdf_bytes
from epic_client import EpicClient, EPIC_FHIR_BASE_URL_DEFAULT

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

# order_new()'s "Load from a saved FHIR order" state (FhirClient.
# parse_order_message_bundle()'s return value), keyed by a random token
# carried through the picker flow's GET links/hidden fields as
# `load_token` — same server-side-dict-keyed-by-a-token pattern as
# _session_clients above. Needed because picking a patient/organisation/
# clinician that didn't auto-resolve is a plain GET, and GETs carry no
# form body — without this, everything a "load" just filled in would be
# gone the moment the user had to pick one of those manually. Same no-
# expiry-beyond-the-life-of-the-process caveat as _session_clients.
_order_load_cache = {}

LOGIN_EXEMPT_ENDPOINTS = {"login", "static"}

#: Usernames allowed to see/use the Admin screens (the bulk clear-downs,
#: the econcur import) — checked against session["username"] exactly as
#: typed at /login (no re-casing/normalising). Everyone else gets a 403
#: on any /admin* route and doesn't see the nav link either (see
#: is_admin_user below / base.html). This is the first real authorization
#: check in the app — /admin previously had none at all ("reachable
#: directly for whoever knows to look" was the whole gate).
ADMIN_USERNAMES = {"xKevin.Mayfield"}


@app.before_request
def _load_client():
    if request.endpoint in LOGIN_EXEMPT_ENDPOINTS:
        return
    fhir_client = _session_clients.get(session.get("sid"))
    if fhir_client is None:
        # request.path is prefix-free (PATH_INFO-based) even when the app is
        # reverse-proxied under a URL prefix (see wsgi.py's PrefixMiddleware)
        # — prepend request.script_root so the post-login redirect below
        # lands back under the prefix instead of at the domain root.
        return redirect(url_for("login", next=request.script_root + request.path))
    g.client = fhir_client
    # request.path (not script_root-prefixed — see above) covers /admin
    # itself plus every /admin/* sub-route (patients confirm/clear-down,
    # orphaned clear-down, econcur import) in one check.
    if request.path.startswith("/admin") and session.get("username") not in ADMIN_USERNAMES:
        abort(403)


# Existing routes/helpers below were all written against a module-level
# `client` — this proxy resolves to the logged-in user's FhirClient
# (set on `g` by _load_client above) so none of them needed to change.
client = LocalProxy(lambda: g.client)


@app.context_processor
def _inject_admin_flag():
    """Exposes is_admin_user/is_production to every template (base.html's
    nav uses is_admin_user to show/hide the Admin link, and is_production
    to hide the "New order" link — order_new() is the actual enforcement,
    this just decides whether to show the link at all). g.client isn't set
    on login-exempt endpoints (see LOGIN_EXEMPT_ENDPOINTS), hence the
    getattr guard rather than using the `client` proxy directly."""
    fhir_client = getattr(g, "client", None)
    return {
        "is_admin_user": session.get("username") in ADMIN_USERNAMES,
        "is_production": fhir_client.is_production() if fhir_client else False,
    }


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


def coding_text(coding):
    """Best-effort human-readable text from a bare FHIR Coding (as opposed
    to code_text()'s CodeableConcept, which wraps a coding list plus its
    own `.text`) — used for Encounter.class, which R4 types as a single
    Coding, not a CodeableConcept."""
    if not coding:
        return "—"
    return coding.get("display") or coding.get("code") or "—"


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


def audit_recorded_date(recorded):
    """Date part of AuditEvent.recorded (an instant, e.g.
    "2026-08-01T10:00:00Z") for the audit trail's separate Date/Time
    columns — a plain split on "T", not a datetime parse/reformat, since
    the FHIR wire format already guarantees that separator."""
    if not recorded:
        return "—"
    return recorded.split("T", 1)[0]


def audit_recorded_time(recorded):
    """Time part of AuditEvent.recorded, as plain HH:MM:SS — see
    audit_recorded_date(). The instant grammar FHIR uses always puts a
    fixed-width HH:MM:SS first, optionally followed by fractional
    seconds and/or a timezone offset (".123", "Z", "+01:00", ...), so
    the first 8 characters after "T" are taken and everything after
    that is dropped rather than displayed."""
    if not recorded or "T" not in recorded:
        return "—"
    return recorded.split("T", 1)[1][:8]


def audit_message_id_short(message_id):
    """Shortens the Message ID column's raw value from
    "<system>.<instance>.<queue>:<id>" down to "<system> <id>" — e.g.
    "RIE.Production.ESBDevelopment:885859" -> "RIE 885859" — keeping just
    what's before the first "." and after the last ":", dropping the
    "."-delimited middle segments and the ":"-prefixed queue name.
    Returns the value unchanged if it doesn't contain both a "." and a
    ":", rather than guessing at a differently-shaped value (including
    the "—" placeholder for "no Message ID entity found")."""
    if not message_id or "." not in message_id or ":" not in message_id:
        return message_id
    prefix = message_id.split(".", 1)[0]
    suffix = message_id.rsplit(":", 1)[-1]
    return f"{prefix} {suffix}"


def reason_code_reference(order):
    """Raw code(s) behind reasonCode — e.g. a Genomic Clinical Indication
    reference number — ignoring display text (see code_value()); joined
    with "; " for multiple entries. ServiceRequest.reasonCode is 0..*
    (a list); Task.reasonCode is 0..1 (a single CodeableConcept) — this
    normalizes either shape into a list before formatting."""
    reason = order.get("reasonCode") or []
    reasons = reason if isinstance(reason, list) else [reason]
    codes = [code_value(rc) for rc in reasons]
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


def slugify(text):
    """Lowercase, non-alphanumeric runs collapsed to single hyphens,
    stripped — turns an organisation's display name into a valid HTML id
    for the ctDNA map's popup links to jump to that organisation's table
    section on the same page. Used both as a Jinja filter (on the exact
    string ctdna.html's <h2> already renders) and directly in
    ctdna_summary() (on the same string, built the identical way via
    _org_display_name()) — same input through the same function is what
    keeps the map's anchors and the sections' ids in sync, not a shared
    lookup table."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "unknown"


app.jinja_env.filters["human_name"] = human_name
app.jinja_env.filters["code_text"] = code_text
app.jinja_env.filters["coding_text"] = coding_text
app.jinja_env.filters["code_value"] = code_value
app.jinja_env.filters["test_directory_code"] = FhirClient.test_directory_code
app.jinja_env.filters["obs_value"] = obs_value
app.jinja_env.filters["specimen_collected"] = specimen_collected
app.jinja_env.filters["specimen_received"] = specimen_received
app.jinja_env.filters["specimen_identifier"] = FhirClient.specimen_identifier
app.jinja_env.filters["placer_identifier"] = FhirClient.placer_identifier
app.jinja_env.filters["filler_identifier"] = FhirClient.filler_identifier
app.jinja_env.filters["encounter_identifier"] = lambda resource: client.hospital_spell_identifier(resource)
app.jinja_env.filters["hospital_spell_identifiers"] = lambda encounter: client.hospital_spell_identifiers(encounter)
app.jinja_env.filters["report_identifier"] = FhirClient.report_identifier
app.jinja_env.filters["reason_code_reference"] = reason_code_reference
app.jinja_env.filters["conclusion_code_reference"] = conclusion_code_reference
app.jinja_env.filters["slugify"] = slugify
app.jinja_env.filters["nhs_or_chi_number"] = FhirClient.nhs_or_chi_number
app.jinja_env.filters["organisation_ods_code"] = FhirClient.organisation_ods_code
app.jinja_env.filters["gmc_number"] = lambda p: FhirClient._identifier_value(p, FhirClient.GMC_NUMBER_SYSTEM)
app.jinja_env.filters["audit_action"] = FhirClient.audit_action_label
app.jinja_env.filters["audit_outcome"] = FhirClient.audit_outcome_label
app.jinja_env.filters["audit_agent_display"] = lambda agent: client.audit_event_agent_display(agent)
app.jinja_env.filters["audit_source_display"] = FhirClient.audit_event_source_display
app.jinja_env.filters["audit_destination_display"] = FhirClient.audit_event_destination_display
app.jinja_env.filters["audit_message_id"] = lambda event: audit_message_id_short(FhirClient.audit_event_message_id(event))
app.jinja_env.filters["audit_correlation_id"] = FhirClient.audit_event_correlation_id
app.jinja_env.filters["audit_query_text"] = FhirClient.audit_event_query_text
app.jinja_env.filters["audit_recorded_date"] = audit_recorded_date
app.jinja_env.filters["audit_recorded_time"] = audit_recorded_time


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
                return redirect(url_for("patient_detail", patient_id=single_patient_id))
        elif patient_id:
            patients = client.search_patients(patient_id=patient_id)
        elif nhs_number:
            patients = client.search_patients(nhs_number=nhs_number)
        elif name:
            patients = client.search_patients(name=name)
    except Exception as e:
        error = str(e)
    patient_numbers = {p["id"]: FhirClient.nhs_or_chi_number(p) for p in patients}
    return render_template("index.html", base_url=client.base_url,
                            patients=patients, error=error,
                            searched_name=name, searched_id=patient_id,
                            searched_nhs=nhs_number, searched_order_number=order_number,
                            identifier_matches=identifier_matches,
                            patient_numbers=patient_numbers)


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
    orders, reports, report_obs, order_organisation = [], [], {}, {}
    order_clinician = {}
    order_performer = {}
    report_interpreters = {}
    report_order = {}
    specimens_by_id = {}
    medical_record_numbers = []
    try:
        patient = client.get_patient(patient_id)
        medical_record_numbers = client.medical_record_numbers(patient)
        orders = client.lab_orders_for_patient(patient_id)
        for o in orders:
            org_name = client.order_organisation(o)
            order_organisation[o["id"]] = _org_display_name(org_name, client.order_organisation_ods(o)) if org_name else "—"
            order_clinician[o["id"]] = client.requesting_clinician_display(o)
            order_performer[o["id"]] = client.performer_display(o)
            for spec in client.resolve_specimens(o):
                specimens_by_id[spec["id"]] = spec
        reports = client.lab_reports_for_patient(patient_id)
        for r in reports:
            report_obs[r["id"]] = client.observations_for_report(r)
            report_interpreters[r["id"]] = client.results_interpreter_display(r)
            # Originating order (via basedOn) for each report card's "View
            # order" link — usually a cache hit, since this patient's own
            # orders were just resolved/cached above via
            # lab_orders_for_patient()'s _include.
            report_order[r["id"]] = client.order_for_report(r)
            for spec in client.resolve_specimens(r):
                specimens_by_id[spec["id"]] = spec
    except Exception as e:
        error = str(e)
    specimens = list(specimens_by_id.values())
    order_chains = client.build_order_chains(orders)
    # Hospital Spell section: distinct Encounters referenced by this
    # patient's orders/reports (encounters_for() dedupes by Encounter id).
    # Resolving them here also warms the reference cache for the orders/
    # reports tables' own "Hospital Spell ID" column below (rendered via
    # the encounter_identifier Jinja filter), so those don't each trigger
    # their own GET. Most recent spell first; an Encounter with no
    # period.start sorts last.
    encounters = sorted(
        client.encounters_for(orders + reports),
        key=lambda e: (e.get("period") or {}).get("start") or "",
        reverse=True,
    )
    return render_template(
        "patient.html", patient_id=patient_id, patient=patient,
        encounters=encounters,
        nhs_number=FhirClient.nhs_number(patient),
        nhs_number_verification_status=FhirClient.nhs_number_verification_status(patient),
        igene_patient_id=FhirClient.igene_patient_identifier(patient),
        medical_record_numbers=medical_record_numbers,
        general_practitioner=client.general_practitioner_display(patient),
        patient_ics=client.patient_ics_display(patient),
        orders=orders, order_chains=order_chains,
        reports=reports, report_obs=report_obs, report_interpreters=report_interpreters,
        report_order=report_order,
        order_organisation=order_organisation, order_clinician=order_clinician,
        order_performer=order_performer,
        specimens=specimens, error=error, is_production=client.is_production(),
    )


@app.route("/patient/<patient_id>/audit-trail")
def patient_audit_trail(patient_id):
    """
    Audit trail for one patient, sourced from the FHIR server's own
    AuditEvent resources (client.audit_events_for_patient()) — who
    accessed or changed this patient's record, when, and what they did.
    Bounded by a start/end date range (?start=&end=, same convention as
    /stats/ctdna/cepheid-results), defaulting to the last 30 days.

    Also filterable by Correlation ID (?correlation_id=) and Message ID
    (?message_id=) — each a `<select>` of the distinct values actually
    present in the date-bounded result set (correlation_ids/message_ids
    below), rather than free text, so the picker can only ever choose a
    value that's really there. Both are exact matches applied
    client-side, combined with AND when both are set, rather than as a
    separate FHIR search — neither is a real search parameter (see
    AUDIT_ENTITY_CORRELATION_ID_CODE's docstring), each is dug out of
    one specific entity's fields the same way its table column is.
    Message ID matches on the same shortened form the column displays
    (audit_message_id_short()), not the raw entity value, so the
    dropdown's options read the same as what's in the table.
    """
    error = None
    patient = None
    events = []
    correlation_ids = []
    message_ids = []
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=30)).isoformat()
    correlation_id = request.args.get("correlation_id", "")
    message_id = request.args.get("message_id", "")
    try:
        patient = client.get_patient(patient_id)
        events = client.audit_events_for_patient(patient_id, start, end)
        correlation_ids = sorted({
            cid for e in events
            if (cid := client.audit_event_correlation_id(e)) != "—"
        })
        message_ids = sorted({
            mid for e in events
            if (mid := audit_message_id_short(FhirClient.audit_event_message_id(e))) != "—"
        })
        if correlation_id:
            events = [e for e in events if client.audit_event_correlation_id(e) == correlation_id]
        if message_id:
            events = [e for e in events if audit_message_id_short(FhirClient.audit_event_message_id(e)) == message_id]
    except Exception as e:
        error = str(e)
    return render_template(
        "audit_trail.html", patient_id=patient_id, patient=patient,
        start=start, end=end, correlation_id=correlation_id,
        correlation_ids=correlation_ids, message_id=message_id,
        message_ids=message_ids, events=events, error=error,
    )


def _patient_postcode(patient):
    """First Address.postalCode on a Patient, or None. Used by the "view
    order form" screen — no existing helper for this since nothing else
    in the app has needed a patient's own postcode before (organisation
    postcodes are geocoded via organisation_geocode() instead)."""
    if not patient:
        return None
    for addr in patient.get("address", []):
        if addr.get("postalCode"):
            return addr["postalCode"]
    return None


@app.route("/order/new", methods=["GET", "POST"])
def order_new():
    """
    Create-order screen for a genomic test order — builds a FHIR message
    Bundle (MessageHeader + ServiceRequest + Specimen + Observations) and
    offers it as a `.json` download; **nothing is written to the FHIR
    server** (see FhirClient.build_order_message_bundle()). Laid out
    after NW GLH's "Genomic Testing Request Form (Rare Disease)"
    (https://mft.nhs.uk/nwglh/documents/test-request-forms/), the IG's
    ServiceRequest/Specimen profiles, and its GenomicTestOrder
    Questionnaire's "Ask At Order Entry" section (all at
    https://nw-gmsa.github.io/).

    Patient and requesting organisation are never freely typed — each is
    a small search-then-pick panel backed by this FHIR server
    (search_patients()/search_organizations()). The requesting clinician
    picker only appears once an organisation is picked, and is *not* a
    free search — practitioners_for_organization() lists only
    Practitioners this FHIR server already links to that organisation
    via an existing PractitionerRole, not every Practitioner on the
    server. All three picks are carried across requests as
    `patient_id`/`org_id`/`practitioner_id` (query params on GET, hidden
    form fields once POSTing the finished order); only once all three
    resolve does the rest of the order/specimen/AOE-questions form
    render.

    Disabled entirely (both GET and POST) when client.is_production() —
    same guard rail as patient_clear_down(): this screen's "Send to RIE"
    button has a real external side effect (submitting a genomic test
    order to North West GLH), so it shouldn't be reachable at all against
    a live production FHIR server, not just have that one button hidden.
    """
    if client.is_production():
        return render_template("order_new.html", production_blocked=True, error=None), 403

    patient_id = request.values.get("patient_id", "").strip()
    org_id = request.values.get("org_id", "").strip()
    practitioner_id = request.values.get("practitioner_id", "").strip()

    error = None
    form_values = {
        "hospital_number": request.form.get("hospital_number", ""),
        "hospital_spell_id": request.form.get("hospital_spell_id", ""),
        "test_code": request.form.get("test_code", ""),
        "order_number": request.form.get("order_number", ""),
        "priority": request.form.get("priority", "routine"),
        "clinical_details": request.form.get("clinical_details", ""),
        "specimen_type": request.form.get("specimen_type", ""),
        "specimen_date": request.form.get("specimen_date", ""),
        "specimen_received_date": request.form.get("specimen_received_date", ""),
        "specimen_placer_id": request.form.get("specimen_placer_id", ""),
        "specimen_accession_number": request.form.get("specimen_accession_number", ""),
        "specimen_tracking_number": request.form.get("specimen_tracking_number", ""),
    }
    # {link_id: submitted value} for every ASK_AT_ORDER_ENTRY_QUESTIONS
    # field that was actually filled in — read here (not just on POST)
    # so a re-rendered form after a validation error keeps them.
    aoe_values = {
        q["link_id"]: request.form.get(f"aoe.{q['link_id']}", "").strip()
        for q in FhirClient.ASK_AT_ORDER_ENTRY_QUESTIONS
    }

    # "Load from a saved FHIR order" — reads a previously downloaded (or
    # otherwise obtained) order bundle, e.g. examples/genomic-order-
    # YHCRABCDORDER.json, and pre-fills the rest of this route's usual
    # state from it (FhirClient.parse_order_message_bundle()) rather
    # than building/submitting anything. patient_id/org_id/
    # practitioner_id can only be set here by actually finding a
    # matching resource on this server — the loaded bundle's own inline
    # Patient/logical-reference Practitioner+Organization are never
    # used directly, same "always picked from this server" rule as the
    # rest of this route. load_notes surfaces anything that couldn't be
    # resolved that way, so the picker sections below explain themselves
    # rather than just silently starting from patient_id="".
    load_notes = []
    load_token = request.values.get("load_token", "").strip()
    # The parsed prefill state from a loaded file (FhirClient.
    # parse_order_message_bundle()'s return value) — either freshly
    # parsed below (a "load" POST) or recovered from _order_load_cache
    # via load_token (any later request in the same picker flow, most
    # importantly the GET a manual patient/organisation/clinician pick
    # triggers). None if no file has been loaded this flow at all.
    loaded = None

    if request.method == "POST" and request.form.get("action") == "load":
        upload = request.files.get("order_file")
        if not upload or not upload.filename:
            error = "Choose a FHIR order JSON file to load."
        else:
            try:
                loaded_bundle = json.loads(upload.read())
                loaded = FhirClient.parse_order_message_bundle(loaded_bundle)
            except Exception as e:
                error = f"Could not read that file as a FHIR order bundle: {e}"
            else:
                load_token = secrets.token_urlsafe(16)
                _order_load_cache[load_token] = loaded
                try:
                    if loaded.get("patient_nhs_number"):
                        matches = client.search_patients(nhs_number=loaded["patient_nhs_number"])
                        if len(matches) == 1:
                            patient_id = matches[0]["id"]
                        else:
                            load_notes.append(
                                f"{'No patient' if not matches else 'More than one patient'} found for NHS "
                                f"number {loaded['patient_nhs_number']} "
                                f"({loaded.get('patient_name') or 'unknown name'}) — search manually below."
                            )
                    if loaded.get("organization_ods"):
                        matches = client.search_organizations(ods_code=loaded["organization_ods"])
                        if len(matches) == 1:
                            org_id = matches[0]["id"]
                        else:
                            load_notes.append(
                                f"Organisation with ODS code {loaded['organization_ods']} "
                                f"({loaded.get('organization_name') or 'unknown name'}) not found on this "
                                f"server — search manually below."
                            )
                    if org_id and loaded.get("practitioner_gmc"):
                        wanted_gmc = FhirClient._format_gmc_number(loaded["practitioner_gmc"])
                        candidates = client.practitioners_for_organization(org_id)
                        match = next(
                            (p for p in candidates if FhirClient._format_gmc_number(
                                FhirClient._identifier_value(p, FhirClient.GMC_NUMBER_SYSTEM)) == wanted_gmc),
                            None,
                        )
                        if match:
                            practitioner_id = match["id"]
                        else:
                            load_notes.append(
                                f"Clinician with GMC {loaded['practitioner_gmc']} "
                                f"({loaded.get('practitioner_name') or 'unknown name'}) not linked to this "
                                f"organisation — pick one below."
                            )
                except Exception as e:
                    error = error or str(e)
    elif load_token:
        loaded = _order_load_cache.get(load_token)

    # Re-applying `loaded` onto form_values/aoe_values is only safe when
    # request.form doesn't already hold the real, possibly-user-edited
    # values — true for the "load" POST itself (its own form has no
    # test_code/specimen_* fields at all) and for every GET in the
    # picker flow (a GET has no form body). It's NOT true for a
    # download/send_esb POST that failed validation and fell through to
    # this same render — request.form there holds what the user actually
    # has in the form right now, which must never be silently overwritten
    # by the originally-loaded values.
    resubmitting_order = request.method == "POST" and request.form.get("action") in ("download", "send_esb")
    if loaded and not resubmitting_order:
        for key in (
            "hospital_number", "hospital_spell_id", "test_code", "order_number", "priority", "clinical_details",
            "specimen_type", "specimen_date", "specimen_received_date",
            "specimen_placer_id", "specimen_accession_number", "specimen_tracking_number",
        ):
            if loaded.get(key):
                form_values[key] = loaded[key]
        aoe_values.update({k: v for k, v in (loaded.get("aoe_values") or {}).items() if v})
    # extra_observations has no form field at all — always safe to pull
    # from `loaded` regardless of resubmitting_order.
    extra_observations = (loaded or {}).get("extra_observations") or []

    if request.method == "POST" and patient_id and org_id and practitioner_id and request.form.get("action") in ("download", "send_esb"):
        hospital_number = form_values["hospital_number"].strip() or None
        hospital_spell_id = form_values["hospital_spell_id"].strip() or None
        test_code = form_values["test_code"].strip()
        order_number = form_values["order_number"].strip() or None
        priority = form_values["priority"] if form_values["priority"] in ("routine", "urgent") else "routine"
        clinical_details = form_values["clinical_details"].strip() or None
        specimen_type = form_values["specimen_type"].strip() or None
        specimen_date = form_values["specimen_date"].strip() or None
        specimen_received_date = form_values["specimen_received_date"].strip() or None
        specimen_placer_id = form_values["specimen_placer_id"].strip() or None
        specimen_accession_number = form_values["specimen_accession_number"].strip() or None
        specimen_tracking_number = form_values["specimen_tracking_number"].strip() or None

        # Both mandatory per the IG's profiles: ServiceRequest.code (1..1)
        # and Specimen.type (1..1) — see build_order_message_bundle().
        if not test_code:
            error = "A test R code is required."
        elif not specimen_type:
            error = "A specimen type is required."
        else:
            try:
                patient = client.resolve_reference({"reference": f"Patient/{patient_id}"})
                organization = client.resolve_reference({"reference": f"Organization/{org_id}"})
                practitioner = client.resolve_reference({"reference": f"Practitioner/{practitioner_id}"})
                if not (patient and organization and practitioner):
                    raise ValueError("Patient, organisation, or clinician could not be re-resolved.")
                bundle = client.build_order_message_bundle(
                    patient=patient, organization=organization, practitioner=practitioner,
                    hospital_number=hospital_number, hospital_spell_id=hospital_spell_id,
                    test_code=test_code, order_number=order_number,
                    priority=priority, clinical_details=clinical_details,
                    specimen_type=specimen_type, specimen_date=specimen_date,
                    specimen_received_date=specimen_received_date,
                    specimen_placer_id=specimen_placer_id, specimen_accession_number=specimen_accession_number,
                    specimen_tracking_number=specimen_tracking_number,
                    aoe_answers={k: v for k, v in aoe_values.items() if v},
                    extra_observations=extra_observations,
                )
            except Exception as e:
                error = str(e)
            else:
                service_request = next(
                    e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "ServiceRequest")
                placer_number = service_request["identifier"][0]["value"]

                if request.form.get("action") == "send_esb":
                    esb_error = None
                    esb_response = None
                    hl7v2_message = None
                    try:
                        esb_response = client.send_order_to_esb(bundle)
                        hl7v2_message = FhirClient.extract_hl7v2_message(esb_response)
                        if hl7v2_message:
                            # HL7 v2 segments are \r-separated on the wire —
                            # normalize to \n so a <pre> block shows one
                            # segment per line instead of one long run.
                            hl7v2_message = hl7v2_message.replace("\r\n", "\n").replace("\r", "\n")
                    except Exception as e:
                        esb_error = str(e)
                    return render_template(
                        "order_send_result.html", error=esb_error, placer_number=placer_number,
                        hl7v2_message=hl7v2_message,
                        response_body=json.dumps(esb_response, indent=2) if esb_response is not None else None,
                    )

                response = Response(json.dumps(bundle, indent=2), mimetype="application/fhir+json")
                response.headers["Content-Disposition"] = f"attachment; filename=genomic-order-{placer_number}.json"
                return response

    # Resolve whatever's already picked (GET query params, or a POST
    # that failed validation — same ids, re-displayed alongside the
    # error and the rest of what was typed). resolve_reference()
    # swallows a bad/stale id into None rather than raising, same as
    # every other reference lookup in this app.
    patient = client.resolve_reference({"reference": f"Patient/{patient_id}"}) if patient_id else None
    organization = client.resolve_reference({"reference": f"Organization/{org_id}"}) if org_id else None
    practitioner = client.resolve_reference({"reference": f"Practitioner/{practitioner_id}"}) if practitioner_id else None

    patient_name_q = request.args.get("patient_name", "").strip()
    patient_nhs_q = request.args.get("patient_nhs", "").strip()
    org_name_q = request.args.get("org_name", "").strip()
    org_ods_q = request.args.get("org_ods", "").strip()

    ready = bool(patient and organization and practitioner)

    patient_results, org_results, practitioner_results = [], [], []
    test_codes, clinical_indications = [], []
    try:
        if not patient and (patient_name_q or patient_nhs_q):
            patient_results = client.search_patients(name=patient_name_q or None, nhs_number=patient_nhs_q or None)
        if not organization and (org_name_q or org_ods_q):
            org_results = client.search_organizations(name=org_name_q or None, ods_code=org_ods_q or None)
        if organization and not practitioner:
            # Not a free search — every practitioner offered here already
            # has a PractitionerRole at the picked organisation (see
            # practitioners_for_organization()). The dropdown itself
            # supports type-to-filter client-side (order_new.html), so
            # the full org-scoped list is fetched once rather than
            # re-querying per keystroke.
            practitioner_results = client.practitioners_for_organization(organization["id"])
        if ready:
            test_codes = FhirClient.genomic_test_directory_codes()
            # Purely a client-side narrowing aid for the R/M code select
            # (order_new.html) — the clinical indication itself is never
            # submitted as its own field, build_order_message_bundle()
            # derives it straight from whichever test_code was picked.
            clinical_indications = FhirClient.genomic_clinical_indications()
            # Pre-fill "Hospital number" from any existing MR identifier
            # already assigned by the requesting organisation — GET only
            # (a failed POST re-render keeps whatever was actually
            # typed/submitted, even if that was cleared to blank).
            if request.method == "GET" and not form_values["hospital_number"]:
                org_ods = FhirClient.organisation_ods_code(organization)
                existing_mrn = next(
                    (m["value"] for m in client.medical_record_numbers(patient) if m["assigner_ods"] == org_ods),
                    None,
                )
                if existing_mrn:
                    form_values["hospital_number"] = existing_mrn
    except Exception as e:
        error = error or str(e)

    return render_template(
        "order_new.html", error=error, form_values=form_values, aoe_values=aoe_values, load_notes=load_notes,
        load_token=load_token,
        extra_observations=extra_observations,
        aoe_questions=FhirClient.ASK_AT_ORDER_ENTRY_QUESTIONS, test_codes=test_codes,
        clinical_indications=clinical_indications,
        specimen_type_codes=FhirClient.SPECIMEN_TYPE_CODES,
        patient=patient, organization=organization, practitioner=practitioner,
        patient_id=patient_id, org_id=org_id, practitioner_id=practitioner_id,
        patient_results=patient_results, org_results=org_results, practitioner_results=practitioner_results,
        patient_name_q=patient_name_q, patient_nhs_q=patient_nhs_q,
        org_name_q=org_name_q, org_ods_q=org_ods_q,
        ready=ready,
    )


@app.route("/order/<order_id>")
def order_view(order_id):
    """
    Single-order "view order form" screen, reached from a patient's
    order table (patient.html links each order's test name here). Laid
    out like the sections on NHS England's Genomic Medicine Service WGS
    Test Request forms (rare disease / cancer —
    https://www.england.nhs.uk/publication/
    nhs-genomic-medicine-service-test-order-forms/): requesting
    organisation/laboratory, patient details, test request details,
    sample details, requesting clinician/contact details.

    Not every field on the paper form has a FHIR equivalent this IG
    populates on ServiceRequest/Patient (ethnicity, HPO terms, family
    members, tumour-specific fields, etc. aren't modelled here) — those
    sections are simply omitted rather than fabricated. This reproduces
    the *available* subset of order data in the same grouping/layout a
    clinician used to the paper form would recognise, not a full replica.
    """
    error = None
    order = None
    patient = None
    specimens = []
    clinical_notes = []
    supporting_info = []
    try:
        order = client.get_order(order_id)
        patient = client.patient_for(order)
        specimens = client.resolve_specimens(order)

        # "Clinical information" — ServiceRequest.note (Annotation: free-text
        # plus optional author/time), the closest FHIR equivalent of the
        # paper form's "Relevant clinical information" field.
        for note in (order.get("note") or []):
            if note.get("text"):
                clinical_notes.append(note)

        # "Supporting information" — ServiceRequest.supportingInfo, a list
        # of References (Observations in this IG, per a real example —
        # ServiceRequest/5743). Resolved via resolve_reference() same as
        # everywhere else, then flattened to one row per actual code/value
        # pair: an Observation's `.component` entries if it has any (same
        # per-component breakdown as the Cepheid screen's component_rows()
        # — a panel-style Observation's real data is in its components,
        # not a single top-level value), otherwise its own top-level
        # code/value[x]. A reference that isn't an Observation, or doesn't
        # resolve at all, still gets a row (code "—", value falls back to
        # the reference's display text/path) rather than being dropped.
        for ref in (order.get("supportingInfo") or []):
            resource = client.resolve_reference(ref)
            if resource and resource.get("resourceType") == "Observation":
                components = resource.get("component") or []
                if components:
                    for c in components:
                        supporting_info.append({
                            "code": code_text(c.get("code")),
                            "value": obs_value(c),
                            "resource_id": resource.get("id"),
                        })
                else:
                    supporting_info.append({
                        "code": code_text(resource.get("code")),
                        "value": obs_value(resource),
                        "resource_id": resource.get("id"),
                    })
            else:
                supporting_info.append({
                    "code": resource.get("resourceType") if resource else None,
                    "value": ref.get("display") or ref.get("reference") or "—",
                    "resource_id": resource.get("id") if resource else None,
                })
    except Exception as e:
        error = str(e)

    return render_template(
        "order_view.html", order=order, patient=patient, specimens=specimens, error=error,
        clinical_notes=clinical_notes, supporting_info=supporting_info,
        nhs_number=FhirClient.nhs_number(patient) if patient else None,
        nhs_number_verification_status=FhirClient.nhs_number_verification_status(patient) if patient else None,
        postcode=_patient_postcode(patient),
        medical_record_numbers=client.medical_record_numbers(patient) if patient else [],
        requester=client.requester_display(order) if order else None,
        requesting_clinician=client.requesting_clinician_display(order) if order else None,
        performer=client.performer_display(order) if order else None,
        placer_assigner=client.placer_identifier_assigner(order) if order else None,
    )


@app.route("/report/<report_id>")
def report_view(report_id):
    """
    Single-report "view report" screen, reached from a patient's report
    card (patient.html links each report's test name here, the same way
    order_view is reached from the orders table). Same lead-with-context
    ordering as order_view: patient details first, then the requesting/
    performing organisations, before the report's own details — see that
    route's docstring.

    Two sections order_view has no equivalent of:
      - **Findings** — the report's actual clinical content: conclusion
        code, results interpreter, linked Observation results (same table
        shape as patient.html's report card), and a link to the source
        PDF(s) (`presentedForm`) — identical markup to patient.html's
        "📄 View report document" link, reused rather than reinvented.
      - **Implications** — a placeholder only. This IG/FHIR server has no
        modelled genomic-implications data (familial risk, cascade-testing
        recommendations, etc.) yet, so this section marks where that would
        go rather than fabricating content.
    """
    error = None
    report = None
    patient = None
    order = None
    specimens = []
    observations = []
    try:
        report = client.get_report(report_id)
        patient = client.patient_for(report)
        # The report's *requesting* organisation isn't on the report
        # itself — DiagnosticReport has no requester — so it's resolved
        # via the originating ServiceRequest, same as the ctDNA/stats
        # "ordering provider" lookups.
        order = client.order_for_report(report)
        specimens = client.resolve_specimens(report)
        observations = client.observations_for_report(report)
    except Exception as e:
        error = str(e)

    return render_template(
        "report_view.html", report=report, patient=patient, order=order,
        specimens=specimens, observations=observations, error=error,
        nhs_number=FhirClient.nhs_number(patient) if patient else None,
        nhs_number_verification_status=FhirClient.nhs_number_verification_status(patient) if patient else None,
        postcode=_patient_postcode(patient),
        medical_record_numbers=client.medical_record_numbers(patient) if patient else [],
        requester=client.requester_display(order) if order else None,
        performer=client.report_organisation(report) if report else None,
        results_interpreter=client.results_interpreter_display(report) if report else None,
        igene_id=client.igene_report_identifier(order, report) if report else None,
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

    Disabled entirely (both GET and POST) when client.is_production() —
    the patient page also hides the link there, but that's just UI; this
    is the actual gate, since a direct hit on this URL would otherwise
    still work.
    """
    if client.is_production():
        return render_template(
            "patient_clear_down_confirm.html", patient_id=patient_id,
            orders=[], reports=[], specimens=[], audit_events=[], error=None,
            production_blocked=True,
        ), 403

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
    orders, reports, specimens, audit_events = [], [], [], []
    try:
        orders = client.lab_orders_for_patient(patient_id)
        reports = client.lab_reports_for_patient(patient_id)
        specimens_by_id = {}
        for resource in orders + reports:
            for spec in client.resolve_specimens(resource):
                specimens_by_id[spec["id"]] = spec
        specimens = list(specimens_by_id.values())
        audit_events = client.audit_events_for_patient(patient_id)
    except Exception as e:
        error = str(e)
    return render_template(
        "patient_clear_down_confirm.html", patient_id=patient_id,
        orders=orders, reports=reports, specimens=specimens,
        audit_events=audit_events, error=error,
    )


@app.route("/admin")
def admin():
    """
    Admin screen: find test/synthetic patients by NHS number range, and
    orphaned (no-patient) ServiceRequests, each with its own destructive
    clear-down action below. This GET only searches/lists — nothing is
    deleted until one of the POST routes below runs.

    Also two AuditEvent clear-down entry points (single patient, or all
    patients within a date range) — both are small forms here that POST
    to their own confirm route rather than listing anything on this GET,
    since (unlike the sections above) there's nothing to preview without
    the admin first choosing a patient/date range.
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
    nw_gmsa_error = None
    nw_gmsa_patients = []
    try:
        nw_gmsa_patients = [
            {
                "id": entry["patient"].get("id"),
                "name": human_name(entry["patient"]),
                "nhs_number": FhirClient.nhs_number(entry["patient"]) or "—",
                "label": entry["label"],
            }
            for entry in client.nw_gmsa_test_patients()
        ]
    except Exception as e:
        nw_gmsa_error = str(e)
    audit_events_end = date.today().isoformat()
    audit_events_start = (date.today() - timedelta(days=30)).isoformat()
    return render_template(
        "admin.html", test_patients=test_patients, orphaned_orders=orphaned_orders, error=error,
        nw_gmsa_patients=nw_gmsa_patients, nw_gmsa_error=nw_gmsa_error,
        nw_gmsa_total=len(FhirClient.NW_GMSA_TEST_PATIENTS),
        audit_events_start=audit_events_start, audit_events_end=audit_events_end,
        tasks_cutoff=date.today().isoformat(),
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


@app.route("/admin/nw-gmsa-patients/confirm", methods=["POST"])
def admin_nw_gmsa_patients_confirm():
    """
    Confirmation page for the selected NW GMSA named test patients
    (FhirClient.NW_GMSA_TEST_PATIENTS — see
    https://github.com/nw-gmsa/Testing/blob/main/MRN-Mapping.md) —
    re-resolves each one and its order/report/specimen/observation/
    related-person/audit-event counts, same preview-then-confirm shape
    as admin_patients_confirm() above, but with the wider set of
    resource types clear_down_patient_full() actually deletes. Nothing
    is deleted here.
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
                "observation_count": len(client.observations_for_patient(pid)),
                "related_person_count": len(client.related_persons_for_patient(pid)),
                "audit_event_count": len(client.audit_events_for_patient(pid)),
            })
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_nw_gmsa_confirm.html", rows=rows, patient_ids=patient_ids, error=error,
    )


@app.route("/admin/nw-gmsa-patients/clear-down", methods=["POST"])
def admin_nw_gmsa_patients_clear_down():
    """Actually deletes the confirmed NW GMSA test patients — Patient
    record plus all Specimens/DiagnosticReports/ServiceRequests/
    Observations/RelatedPersons/AuditEvents — via
    clear_down_patient_full(), the only route in this pair that mutates
    anything."""
    patient_ids = request.form.getlist("patient_id")
    error = None
    deleted, failed = [], []
    try:
        for pid in patient_ids:
            result = client.clear_down_patient_full(pid)
            deleted.extend(result["deleted"])
            failed.extend(result["failed"])
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html", title="NW GMSA test patient clear-down result",
        deleted=deleted, failed=failed, error=error,
        back_url=request.script_root + "/admin", back_label="Back to admin",
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


def _resolve_patient_by_id_or_nhs_number(value):
    """Resolves a free-typed admin-screen value to one Patient — tries it
    as a raw Patient id first (search_patients(patient_id=...) already
    swallows a bad/unknown id into []), then as an NHS number if that
    comes back empty. Returns the first match, or None. Deliberately
    simple (no disambiguation UI) since search_patients(nhs_number=...)
    already matches on a specific identifier value, so more than one
    result would be unusual — same "take the first, don't overbuild"
    stance as the rest of this admin screen's small forms."""
    patients = client.search_patients(patient_id=value)
    if not patients:
        patients = client.search_patients(nhs_number=value)
    return patients[0] if patients else None


@app.route("/admin/audit-events/patient/confirm", methods=["POST"])
def admin_audit_events_patient_confirm():
    """
    Confirmation page for deleting one patient's AuditEvent history —
    resolves the typed patient id/NHS number and counts their AuditEvents
    (client.audit_events_for_patient(), no date bound — see that
    method's docstring for why unbounded is safe here) before showing a
    delete button, same preview-then-confirm shape as the NHS-number-
    range patient clear-down above. Nothing is deleted here.
    """
    value = request.form.get("patient", "").strip()
    error = None
    patient = None
    event_count = 0
    try:
        patient = _resolve_patient_by_id_or_nhs_number(value)
        if patient:
            event_count = len(client.audit_events_for_patient(patient["id"]))
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_audit_events_patient_confirm.html", value=value, patient=patient,
        event_count=event_count, error=error,
    )


@app.route("/admin/audit-events/patient/clear-down", methods=["POST"])
def admin_audit_events_patient_clear_down():
    """Actually deletes every AuditEvent for the confirmed patient
    (client.clear_down_audit_events_for_patient()) — the AuditEvent
    resources only, same as the patient page's own clear-down doesn't
    touch the Patient record itself unless separately opted in."""
    patient_id = request.form.get("patient_id", "")
    error = None
    result = {"deleted": [], "failed": []}
    try:
        result = client.clear_down_audit_events_for_patient(patient_id)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html", title="Patient AuditEvent clear-down result",
        deleted=result["deleted"], failed=result["failed"], error=error,
        back_url=request.script_root + f"/patient/{patient_id}/audit-trail",
        back_label="Back to patient audit trail",
    )


@app.route("/admin/audit-events/all/confirm", methods=["POST"])
def admin_audit_events_all_confirm():
    """
    Confirmation page for deleting AuditEvents system-wide (every
    patient) within a date range — counts what's there
    (client.audit_events_in_range()) before showing a delete button.
    Bounded by start/end rather than truly all-time, both to avoid the
    413 risk an unbounded AuditEvent query carries on this server (see
    audit_events_in_range()'s docstring) and so this bulk action has a
    deliberately chosen blast radius rather than nuking the entire audit
    history in one click. Nothing is deleted here.
    """
    start = request.form.get("start") or (date.today() - timedelta(days=30)).isoformat()
    end = request.form.get("end") or date.today().isoformat()
    error = None
    event_count = 0
    try:
        event_count = len(client.audit_events_in_range(start, end))
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_audit_events_all_confirm.html", start=start, end=end,
        event_count=event_count, error=error,
    )


@app.route("/admin/audit-events/all/clear-down", methods=["POST"])
def admin_audit_events_all_clear_down():
    """Actually deletes every AuditEvent system-wide within the confirmed
    date range (client.clear_down_audit_events_in_range())."""
    start = request.form.get("start", "")
    end = request.form.get("end", "")
    error = None
    result = {"deleted": [], "failed": []}
    try:
        result = client.clear_down_audit_events_in_range(start, end)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html", title="AuditEvent clear-down result (all patients)",
        deleted=result["deleted"], failed=result["failed"], error=error,
    )


@app.route("/admin/tasks/confirm", methods=["POST"])
def admin_tasks_confirm():
    """
    Confirmation page for deleting Task resources system-wide whose
    `meta.lastUpdated` is before the given cutoff date — counts what's
    there (client.tasks_last_updated_before()) before showing a delete
    button. Nothing is deleted here.
    """
    cutoff = request.form.get("cutoff") or date.today().isoformat()
    error = None
    task_count = 0
    try:
        task_count = len(client.tasks_last_updated_before(cutoff))
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_tasks_confirm.html", cutoff=cutoff, task_count=task_count, error=error,
    )


@app.route("/admin/tasks/clear-down", methods=["POST"])
def admin_tasks_clear_down():
    """Actually deletes every Task system-wide last updated before the
    confirmed cutoff (client.clear_down_tasks_last_updated_before())."""
    cutoff = request.form.get("cutoff", "")
    error = None
    result = {"deleted": [], "failed": []}
    try:
        result = client.clear_down_tasks_last_updated_before(cutoff)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html", title="Task clear-down result",
        deleted=result["deleted"], failed=result["failed"], error=error,
    )


# econcur import job state — a single in-memory slot, same simplicity as
# _session_clients (no job queue in this app). Only one import can run at
# a time; the background thread below is the only writer, the two routes
# after it are readers/starters. Not persisted across a process restart,
# same caveat as _session_clients.
_econcur_import_job = {"state": "idle"}


def _run_econcur_import(fhir_client, apply):
    """Runs in a background thread (started by admin_econcur_import_start()
    below) so the request that triggers it can return immediately instead
    of blocking for however long a ~75,000-row econcur.csv import takes.
    Takes `fhir_client` as a plain argument rather than reading app.client
    — there's no request context in a background thread for the `g.client`
    LocalProxy to resolve against, so the caller resolves the real
    FhirClient object first (client._get_current_object()) and passes it
    in directly."""
    _econcur_import_job.update({
        "state": "running", "stage": "downloading econcur.csv", "apply": apply,
        "processed": 0, "total": None, "result": None, "error": None,
    })

    def progress(processed, total):
        _econcur_import_job["stage"] = "importing"
        _econcur_import_job["processed"] = processed
        _econcur_import_job["total"] = total

    try:
        csv_text = fhir_client.fetch_econcur_csv()
        result = fhir_client.import_econcur(csv_text, apply=apply, progress=progress)
        _econcur_import_job["state"] = "done"
        _econcur_import_job["result"] = result
    except Exception as e:
        _econcur_import_job["state"] = "error"
        _econcur_import_job["error"] = str(e)


@app.route("/admin/econcur-import")
def admin_econcur_import():
    """
    Admin screen for importing NHS ODS's econcur.csv (the "English
    Hospital Consultants" CSV export — see
    https://digital.nhs.uk/services/organisation-data-service/data-search-and-export/csv-downloads/miscellaneous)
    as Practitioner + PractitionerRole resources (FhirClient.import_econcur()).
    GET only shows the form plus whatever the current/last job's status is
    — the run itself always happens in the background thread started by
    the POST route below, never inline in a request, since the full
    export is tens of thousands of rows.

    Dry run vs apply is the same convention as
    scripts/fix_organization_names.py's --apply flag: dry run (the
    default button) computes and shows exactly what apply would do
    without writing anything, so the counts can be sanity-checked before
    committing tens of thousands of creates/updates to real FHIR data.

    The page auto-refreshes every few seconds while a job is running
    (see the template) so progress is visible without needing any JS.
    """
    return render_template("admin_econcur_import.html", job=_econcur_import_job)


@app.route("/admin/econcur-import/start", methods=["POST"])
def admin_econcur_import_start():
    """Starts a background econcur import (see _run_econcur_import above)
    and redirects straight back to the status page. Refuses to start a
    second job while one is already running - this app has only one
    in-memory job slot, and two concurrent imports would race on the same
    preloaded Practitioner/Organization/PractitionerRole lookup dicts."""
    if _econcur_import_job.get("state") != "running":
        apply = request.form.get("apply") == "1"
        fhir_client = client._get_current_object()
        threading.Thread(target=_run_econcur_import, args=(fhir_client, apply), daemon=True).start()
    return redirect(url_for("admin_econcur_import"))


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
    """
    Active filler-order work items — moved from ServiceRequest to Task
    (Task.status=requested, Task.intent=filler-order), owned by a given
    Organization (Task.owner) rather than filtered by a "Requested by"
    organisation dropdown the way the old ServiceRequest-based version
    was: the fetch itself is now scoped by owner (see
    client.active_filler_tasks()), defaulting to
    FhirClient.DEFAULT_TASK_OWNER_ODS_CODE ("K1S6S", "Liverpool GLH").

    This deployment's Task usage for filler-order work items is new,
    unconfirmed territory (see resolve_task_focus_order()'s docstring) —
    the Filler ID column falls back to the Task's focus ServiceRequest's
    own `identifier` wherever the Task itself doesn't carry one directly.

    The Business Status column reads Task.businessStatus (the workflow
    status a lab attaches to a piece of work, distinct from Task.status'
    fixed FHIR request-lifecycle state) — this deployment doesn't
    populate it yet, so it initially falls back to showing the Genomic
    Test Directory code instead (from the Task's own `code`, or the
    focus order's), as a placeholder until real business-status values
    exist to show.
    """
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=30)).isoformat()
    owner_ods = request.args.get("owner") or FhirClient.DEFAULT_TASK_OWNER_ODS_CODE
    selected_status = request.args.get("status", FhirClient.DEFAULT_TASK_STATUS)
    sort = request.args.get("sort", "")

    error = None
    owner_org = None
    rows = []
    try:
        owner_matches = client.search_organizations(ods_code=owner_ods)
        owner_org = owner_matches[0] if owner_matches else None
        if owner_org:
            for t in client.active_filler_tasks(owner_org["id"], status=selected_status, start=start, end=end):
                focus_order = client.resolve_task_focus_order(t)
                identifier_source = t if t.get("identifier") else (focus_order or {})
                requester_source = t if t.get("requester") else (focus_order or {})
                patient = client.patient_for(t) or (client.patient_for(focus_order) if focus_order else None)
                code = t.get("code") or (focus_order.get("code") if focus_order else None)
                business_status_source = t.get("businessStatus") or code
                business_status = (
                    FhirClient.test_directory_code(business_status_source)
                    or code_text(business_status_source)
                    or "—"
                )
                rows.append({
                    "id": t.get("id"),
                    "focus_order_id": focus_order.get("id") if focus_order else None,
                    "patient_id": patient.get("id") if patient else None,
                    "patient_name": human_name(patient) if patient else "Unknown",
                    "status": t.get("status") or "—",
                    "business_status": business_status,
                    "intent": t.get("intent") or "—",
                    "ordered": t.get("authoredOn") or "—",
                    "requested_by": client.requester_display(requester_source),
                    "owner": client.owner_display(t),
                    "filler_id": FhirClient.filler_identifier(identifier_source) or "—",
                    "group_identifier": FhirClient.task_group_identifier(t) or "—",
                })
    except Exception as e:
        error = str(e)

    if sort in ("ordered_asc", "ordered_desc"):
        rows.sort(key=lambda r: r["ordered"] or "", reverse=(sort == "ordered_desc"))

    owner_name = owner_org.get("name") if owner_org else None
    owner_display_name = (_org_display_name(owner_name, owner_ods) if owner_name else owner_ods) if owner_org else None

    return render_template(
        "work_orders.html", rows=rows, sort=sort,
        start=start, end=end, owner_ods=owner_ods, owner_display_name=owner_display_name,
        owner_found=bool(owner_org),
        statuses=FhirClient.TASK_STATUS_VALUES, selected_status=selected_status,
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
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=6)).isoformat()

    orders, order_chains, order_requester, order_performer, order_patient, error = _order_worklist(
        lambda: client.active_placer_orders(start, end))

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
        start=start, end=end,
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

    Scoped to the same start/end/org/test filter the page was showing when
    the button was clicked (passed through as hidden fields) — otherwise
    the displayed "N of the orders above" count could disagree with how
    many this actually deletes.
    """
    start = request.form.get("start", "")
    end = request.form.get("end", "")
    selected_org = request.form.get("org", "")
    selected_test = request.form.get("test", "")
    error = None
    result = {"deleted": [], "failed": []}
    try:
        orders = client.active_placer_orders(start, end)
        order_organisation = {o["id"]: (client.order_organisation(o) or "Unknown") for o in orders}
        order_test = {o["id"]: (FhirClient.test_directory_code(o.get("code")) or "—") for o in orders}
        orders = _filter_orders_by_org_and_test(orders, order_organisation, order_test, selected_org, selected_test)
        result = client.clear_down_orders_with_unknown_patient(orders)
    except Exception as e:
        error = str(e)
    return render_template(
        "admin_clear_down_result.html", title="Unknown-patient test orders clear-down result",
        back_url=url_for("test_orders", start=start, end=end, org=selected_org, test=selected_test),
        back_label="Back to test orders",
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


def _org_display_name(name, ods):
    """"Name (ODS)" if ods is known, else just the name — the exact
    format ctdna_summary() groups rows by and the ctDNA map's popups link
    to (via slugify(), see above), so both sides agree on one identity
    string per organisation."""
    return f"{name} ({ods})" if ods else name


def _requester_code(resource):
    """The raw code/reference identifying a ServiceRequest's `requester` —
    used on stats_organisation()'s "Unknown" group, since the display
    name having failed to resolve is exactly when this raw value is the
    only lead left to trace an order back to its real requester. Prefers
    requester.identifier.value (a logical reference's own code) over the
    literal .reference path, since that's more directly "the code of the
    requester"; falls back to .reference, then .display. Returns "—" if
    resource is None (e.g. a report with no basedOn link at all) or has
    no requester."""
    if not resource:
        return "—"
    requester = resource.get("requester") or {}
    identifier = requester.get("identifier") or {}
    if identifier.get("value"):
        return identifier["value"]
    if requester.get("reference"):
        return requester["reference"]
    if requester.get("display"):
        return requester["display"]
    return "—"


def _order_report_row(order, report):
    """One order (+ its linked report, if any) in the ctDNA screen's row
    shape — Patient, Status (Completed/Outstanding, from
    order.status == "completed"), Test code, Conclusion code, Order
    date, Sample collected/received, Date reported, iGene report ID,
    Placer number. Shared by ctdna_summary() and stats_organisation(),
    which both show orders enriched with their linked report's data this
    same way — `report` may be None (no linked report resolved)."""
    is_completed = order.get("status") == "completed"
    reported_date = None
    if report:
        reported_date = (report.get("issued") or report.get("effectiveDateTime") or "")[:10] or None

    specimens = client.resolve_specimens(order)
    if not specimens and report:
        specimens = client.resolve_specimens(report)
    specimen = specimens[0] if specimens else None
    patient = client.patient_for(order)
    conclusion = "; ".join(
        code_text(cc) for cc in (report.get("conclusionCode") or [])
    ) if report else ""

    return {
        "order_id": order.get("id"),
        "report_id": report.get("id") if report else None,
        "patient_id": patient.get("id") if patient else None,
        "patient_name": human_name(patient) if patient else "Unknown",
        "requester": client.requesting_clinician_display(order),
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
        "spell_id": client.hospital_spell_identifier(order) or (client.hospital_spell_identifier(report) if report else None) or "—",
    }


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
        order_by_org=group_count(order_rows, "organisation"),
        order_by_indication=group_count(order_rows, "indication"),
        order_by_ics=group_count(order_rows, "ics"),
        order_by_country=group_count(order_rows, "country"),
        order_map_points=order_map_points,
        order_map_unmapped_count=order_map_unmapped_count,
        order_ics_map_html=order_ics_map_html,
        order_ics_map_unmatched_count=order_ics_map_unmatched_count,
        order_ics_map_unmatched_names=order_ics_map_unmatched_names,
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


@app.route("/stats/organisation")
def stats_organisation():
    """
    Drill-down from /stats's "Orders by requesting organisation" and
    "Reports by ordering provider" tables: one organisation's orders and
    reports (same [start, end] range as the /stats page linked from —
    carried through as query params, not re-defaulted here). Both source
    tables link here — "requesting organisation" (orders) and "ordering
    provider" (reports) are the same concept from two different resource
    types, so one screen covers both.

    Two views of the same data: the "by test directory code" tables
    (unchanged from before — a single count per code), and a detailed row
    list in the same shape as /ctdna's table (Patient/Status/Test code/
    Conclusion code/Order date/Sample collected/Sample received/Date
    reported/iGene report ID/Placer number), via the shared
    _order_report_row() helper.

    Reuses orders_in_range()/reports_in_range() (same date-bounded,
    _include-bundled queries /stats itself uses) and filters down to this
    one organisation client-side — there's no FHIR search param for
    "requesting organisation display name" to push this down to the
    query itself. Unlike ctdna_orders(), these two queries don't come
    with a ready-made order-id -> report lookup, so one is built here
    from the fetched `reports` list (same "keep the most recently issued"
    logic as ctdna_orders()'s reports_by_order_id).
    """
    org = request.args.get("org", "")
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=6)).isoformat()

    error = None
    rows = []
    order_by_test, report_by_test = [], []
    try:
        orders = client.orders_in_range(start, end)
        reports = client.reports_in_range(start, end)

        report_by_order_id = {}
        for r in reports:
            ordering_order = client.order_for_report(r)
            if not ordering_order:
                continue
            oid = ordering_order.get("id")
            existing = report_by_order_id.get(oid)
            issued = r.get("issued") or r.get("effectiveDateTime") or ""
            existing_issued = (existing.get("issued") or existing.get("effectiveDateTime") or "") if existing else ""
            if existing is None or issued > existing_issued:
                report_by_order_id[oid] = r

        matched_order_ids = set()
        order_test_rows, report_test_rows = [], []
        for o in orders:
            if (client.order_organisation(o) or "Unknown") != org:
                continue
            matched_order_ids.add(o.get("id"))
            order_test_rows.append({"test": FhirClient.test_directory_code(o.get("code")) or "—"})
            row = _order_report_row(o, report_by_order_id.get(o.get("id")))
            if org == "Unknown":
                row["requester_code"] = _requester_code(o)
            rows.append(row)

        # Reports whose ordering provider matches org but whose order
        # either didn't resolve, or wasn't itself in orders_in_range()'s
        # result for this range (e.g. authored outside [start, end] while
        # the report itself is within range) — still shown, with
        # order-only fields left as "—", rather than silently dropped.
        for r in reports:
            ordering_order = client.order_for_report(r)
            ordering_provider = (client.order_organisation(ordering_order) if ordering_order else None) or "Unknown"
            if ordering_provider != org:
                continue
            report_test_rows.append({"test": FhirClient.test_directory_code(r.get("code")) or "—"})
            if ordering_order and ordering_order.get("id") in matched_order_ids:
                continue  # already emitted above, via its order
            patient = client.patient_for(r)
            row = {
                "order_id": ordering_order.get("id") if ordering_order else None,
                "report_id": r.get("id"),
                "patient_id": patient.get("id") if patient else None,
                "patient_name": human_name(patient) if patient else "Unknown",
                "requester": client.requesting_clinician_display(ordering_order) if ordering_order else "—",
                "test": FhirClient.test_directory_code(r.get("code")) or "—",
                "status": r.get("status") or "—",
                "order_date": (ordering_order.get("authoredOn") if ordering_order else None) or "—",
                "collected_date": "—",
                "received_date": "—",
                "reported_date": (r.get("issued") or r.get("effectiveDateTime") or "")[:10] or "—",
                "placer_id": (FhirClient.placer_identifier(ordering_order) if ordering_order else None) or "—",
                "igene_id": client.igene_report_identifier(ordering_order or {}, r) or "—",
                "conclusion": "; ".join(code_text(cc) for cc in (r.get("conclusionCode") or [])) or "—",
                "spell_id": (client.hospital_spell_identifier(ordering_order) if ordering_order else None) or client.hospital_spell_identifier(r) or "—",
            }
            if org == "Unknown":
                row["requester_code"] = _requester_code(ordering_order)
            rows.append(row)

        order_by_test = group_count(order_test_rows, "test")
        report_by_test = group_count(report_test_rows, "test")
    except Exception as e:
        error = str(e)

    return render_template(
        "stats_organisation.html", org=org, start=start, end=end, error=error,
        rows=rows, order_by_test=order_by_test, report_by_test=report_by_test,
        show_requester_code=(org == "Unknown"),
    )


@app.route("/stats/ics")
def stats_ics():
    """
    Same drill-down idea as stats_organisation() above, but for /stats's
    "Orders by requesting organisation's ICS" and "Reports by ordering
    provider's ICS" tables — grouped by ICS instead of organisation name.
    ICS here is organisation_ics() (geocoded from the requesting/ordering
    organisation's own postcode), the same derivation /stats itself uses
    for these two tables — not Patient.managingOrganization, which is a
    different "ICS" concept used elsewhere on /stats (the country/ICS
    breakdown further down the page, off the patient rather than the
    organisation).

    Same two views as stats_organisation(): "by test directory code"
    counts, and a detailed row list (_order_report_row() shape) with
    linked iGene report ID / Placer number columns — see stats_ics.html
    for why those two specifically are clickable here.
    """
    ics = request.args.get("ics", "")
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=6)).isoformat()

    error = None
    rows = []
    order_by_test, report_by_test = [], []
    try:
        orders = client.orders_in_range(start, end)
        reports = client.reports_in_range(start, end)

        report_by_order_id = {}
        for r in reports:
            ordering_order = client.order_for_report(r)
            if not ordering_order:
                continue
            oid = ordering_order.get("id")
            existing = report_by_order_id.get(oid)
            issued = r.get("issued") or r.get("effectiveDateTime") or ""
            existing_issued = (existing.get("issued") or existing.get("effectiveDateTime") or "") if existing else ""
            if existing is None or issued > existing_issued:
                report_by_order_id[oid] = r

        matched_order_ids = set()
        order_test_rows, report_test_rows = [], []
        for o in orders:
            order_ics = client.organisation_ics(client.order_organisation_resource(o)) or "Unknown"
            if order_ics != ics:
                continue
            matched_order_ids.add(o.get("id"))
            order_test_rows.append({"test": FhirClient.test_directory_code(o.get("code")) or "—"})
            rows.append(_order_report_row(o, report_by_order_id.get(o.get("id"))))

        # Reports whose ordering provider's ICS matches but whose order
        # either didn't resolve, or wasn't itself in orders_in_range()'s
        # result for this range — still shown, order-only fields "—",
        # same reasoning as stats_organisation().
        for r in reports:
            ordering_order = client.order_for_report(r)
            ordering_org_resource = client.order_organisation_resource(ordering_order) if ordering_order else None
            report_ics = client.organisation_ics(ordering_org_resource) or "Unknown"
            if report_ics != ics:
                continue
            report_test_rows.append({"test": FhirClient.test_directory_code(r.get("code")) or "—"})
            if ordering_order and ordering_order.get("id") in matched_order_ids:
                continue  # already emitted above, via its order
            patient = client.patient_for(r)
            rows.append({
                "order_id": ordering_order.get("id") if ordering_order else None,
                "report_id": r.get("id"),
                "patient_id": patient.get("id") if patient else None,
                "patient_name": human_name(patient) if patient else "Unknown",
                "requester": client.requesting_clinician_display(ordering_order) if ordering_order else "—",
                "test": FhirClient.test_directory_code(r.get("code")) or "—",
                "status": r.get("status") or "—",
                "order_date": (ordering_order.get("authoredOn") if ordering_order else None) or "—",
                "collected_date": "—",
                "received_date": "—",
                "reported_date": (r.get("issued") or r.get("effectiveDateTime") or "")[:10] or "—",
                "placer_id": (FhirClient.placer_identifier(ordering_order) if ordering_order else None) or "—",
                "igene_id": client.igene_report_identifier(ordering_order or {}, r) or "—",
                "conclusion": "; ".join(code_text(cc) for cc in (r.get("conclusionCode") or [])) or "—",
                "spell_id": (client.hospital_spell_identifier(ordering_order) if ordering_order else None) or client.hospital_spell_identifier(r) or "—",
            })

        order_by_test = group_count(order_test_rows, "test")
        report_by_test = group_count(report_test_rows, "test")
    except Exception as e:
        error = str(e)

    return render_template(
        "stats_ics.html", ics=ics, start=start, end=end, error=error,
        rows=rows, order_by_test=order_by_test, report_by_test=report_by_test,
    )


#: Default "low data quality score" cutoff for the /data-quality report's
#: "Low data quality scores by source" check — rows scoring this or
#: below are flagged. Overridable per-request via ?score_threshold=.
DEFAULT_SCORE_THRESHOLD = 8


def _data_quality_params():
    """(start, end, score_threshold) from query params — shared by
    data_quality() and data_quality_pdf() so the date-range defaulting
    (last 30 days, same convention as /stats/`/ctdna`) and the lenient
    int() fallback for a malformed ?score_threshold= live in one place,
    not duplicated between the HTML and PDF routes."""
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=30)).isoformat()
    try:
        score_threshold = int(request.args.get("score_threshold", DEFAULT_SCORE_THRESHOLD))
    except ValueError:
        score_threshold = DEFAULT_SCORE_THRESHOLD
    return start, end, score_threshold


def _build_data_quality_report(start, end, score_threshold):
    """Builds the IrisClient report for the given range/threshold,
    returning (report, error) with report=None whenever there's an
    error to show instead — either an exception from IrisClient itself
    (bad credentials, connection failure, missing env vars) or a report
    dict that came back carrying its own top-level "error" (e.g. the
    table wasn't found). Shared by data_quality() and data_quality_pdf()
    so the HTML page and its PDF download can never show different data
    for the same query params."""
    report = None
    error = None
    try:
        iris = IrisClient(user=client.user, password=client.password)
        report = iris.build_report(start=start, end=end, score_threshold=score_threshold)
        if report and report.get("error"):
            error = report["error"]
            report = None
    except Exception as e:
        error = str(e)
    return report, error


@app.route("/data-quality")
def data_quality():
    """
    Data quality report for RIE.PatientDemographics — a table in the
    InterSystems IRIS database behind ENTERPRISESERVICEBUS, not FHIR.
    There's no FHIR-side equivalent of "how complete/consistent is the
    source demographics table" (Patient resources are downstream of it),
    so this queries IRIS SQL directly via IrisClient rather than going
    through client (the FhirClient proxy every other route uses).

    Reuses the logged-in user's own FHIR credentials
    (client.user/client.password) instead of a second login screen —
    confirmed to work against both the FHIR API and this IRIS database on
    this deployment. If that ever stops being true, this route (and only
    this route) would need its own login.

    `start`/`end` (same `?start=&end=` query-param convention as
    /stats/`/ctdna`) default to the last 30 days and scope the whole
    report to rows whose LastUpdated-like column falls in that range
    (IrisClient.build_report() detects the column by name — see
    LAST_UPDATED_PATTERNS in iris_client.py). `score_threshold`
    (?score_threshold=, default DEFAULT_SCORE_THRESHOLD) controls the
    "Low data quality scores by source" check's cutoff; invalid input
    falls back to the default rather than erroring the whole page.

    See data_quality_pdf() for the "Download as PDF" version of this
    same report.
    """
    start, end, score_threshold = _data_quality_params()
    report, error = _build_data_quality_report(start, end, score_threshold)
    return render_template(
        "data_quality.html", report=report, error=error,
        iris_host=IrisClient.HOST, iris_namespace=IrisClient.NAMESPACE,
        start=start, end=end, score_threshold=score_threshold,
    )


@app.route("/data-quality/pdf")
def data_quality_pdf():
    """
    The same report data_quality() renders, as a downloadable PDF
    (pdf_report.quality_report_pdf_bytes()) — same query params
    (`_data_quality_params()`/`_build_data_quality_report()` are shared
    with the HTML route), so "Download as PDF" on the report page
    (data_quality.html carries the current start/end/score_threshold
    through as a query string on that link) produces a PDF matching
    what's currently on screen.
    """
    start, end, score_threshold = _data_quality_params()
    report, error = _build_data_quality_report(start, end, score_threshold)
    pdf_bytes = quality_report_pdf_bytes(
        report, start, end, score_threshold, IrisClient.HOST, IrisClient.NAMESPACE, error=error)
    filename = f"data-quality-report-{start}-to-{end}.pdf"
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/ctdna")
def ctdna_summary():
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=30)).isoformat()

    error = None
    rows = []
    try:
        orders, reports_by_order = client.ctdna_orders(start=start, end=end)
        mapped_orders = []  # orders that survive the completed-date filter below — see map building after the loop

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

            if is_completed:
                # Bound "completed" by when it was reported (the actual
                # completion event), falling back to order date if no
                # report resolved.
                reported_date = (report.get("issued") or report.get("effectiveDateTime") or "")[:10] if report else None
                completion_date = reported_date or (order.get("authoredOn") or "")[:10]
                if not completion_date or completion_date < start or completion_date > end:
                    continue

            mapped_orders.append(order)
            org_resource = client.order_organisation_resource(order)
            org_name = (org_resource.get("name") if org_resource else None) or client.order_organisation(order) or "Unknown"
            ods_code = client.order_organisation_ods(order)
            organisation = _org_display_name(org_name, ods_code)

            row = _order_report_row(order, report)
            row["organisation"] = organisation
            rows.append(row)

        # Most recently ordered first within each group; Outstanding above
        # Completed (two stable sorts so both orderings hold at once).
        rows.sort(key=lambda r: r["order_date_raw"], reverse=True)
        rows.sort(key=lambda r: r["status"] != "Outstanding")
        rows_by_org = group_rows_by_organisation(rows)

        # Map of orders by ordering provider (NHS Trust), same points/popup
        # shape /stats's "Orders by requesting organisation" map uses —
        # orders_by_organisation_geocoded() is shared, unmodified. Built
        # from mapped_orders (the orders that actually made it into `rows`
        # after the completed-date filter above), not the raw `orders`
        # list, so the map agrees with what the tables below it show.
        # Each point's "anchor" is the slugified org display name — the
        # exact same string, run through the exact same slugify() Jinja
        # filter, is set as each <h2>'s id in ctdna.html, so a popup's
        # "View NHS Trust details" link jumps straight to that
        # organisation's table section on this page.
        org_geo = orders_by_organisation_geocoded(mapped_orders)
        order_map_points = [
            {**g, "anchor": slugify(_org_display_name(g["name"], g["ods"]))}
            for g in org_geo if g["lat"] is not None and g["lon"] is not None
        ]
        order_map_unmapped_count = sum(g["count"] for g in org_geo if g["lat"] is None)

        # Orders by ICS, same choropleth /stats's "Orders by requesting
        # organisation's ICS" uses (ics_choropleth_html() is shared,
        # unmodified) — also built from mapped_orders, same reasoning as
        # the NHS Trust map above (agree with what the tables show).
        # organisation_ics() is a point-in-polygon lookup off each order's
        # already-resolved Organization resource (cached from
        # orders_by_organisation_geocoded() just above), not
        # Patient.managingOrganization.
        order_ics_rows = [
            {"ics": client.organisation_ics(client.order_organisation_resource(o)) or "Unknown"}
            for o in mapped_orders
        ]
        order_by_ics = group_count(order_ics_rows, "ics")
        order_ics_map_html, order_ics_map_unmatched_count, order_ics_map_unmatched_names = ics_choropleth_html(order_by_ics)

        # Reports by ICS — same idea, but counting the orders that
        # actually have a linked report (reports_by_order), not every
        # mapped order. ctDNA has no independent "reports" list the way
        # /stats does (reports_by_order maps at most one — the most
        # recently issued — report per order id), so "one order with a
        # report" stands in for "one report" here.
        report_backed_orders = [o for o in mapped_orders if reports_by_order.get(o.get("id"))]
        report_ics_rows = [
            {"ics": client.organisation_ics(client.order_organisation_resource(o)) or "Unknown"}
            for o in report_backed_orders
        ]
        report_by_ics = group_count(report_ics_rows, "ics")
        report_ics_map_html, report_ics_map_unmatched_count, report_ics_map_unmatched_names = ics_choropleth_html(report_by_ics)
    except Exception as e:
        error = str(e)
        rows_by_org = []
        order_map_points, order_map_unmapped_count = [], 0
        order_by_ics = []
        order_ics_map_html, order_ics_map_unmatched_count, order_ics_map_unmatched_names = None, 0, []
        report_by_ics = []
        report_ics_map_html, report_ics_map_unmatched_count, report_ics_map_unmatched_names = None, 0, []

    return render_template(
        "ctdna.html", rows_by_org=rows_by_org, error=error, start=start, end=end,
        order_map_points=order_map_points, order_map_unmapped_count=order_map_unmapped_count,
        order_by_ics=order_by_ics, order_ics_map_html=order_ics_map_html,
        order_ics_map_unmatched_count=order_ics_map_unmatched_count,
        order_ics_map_unmatched_names=order_ics_map_unmatched_names,
        report_by_ics=report_by_ics, report_ics_map_html=report_ics_map_html,
        report_ics_map_unmatched_count=report_ics_map_unmatched_count,
        report_ics_map_unmatched_names=report_ics_map_unmatched_names,
    )


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
                "spell_id": (client.hospital_spell_identifier(order) if order else None) or client.hospital_spell_identifier(report) or "—",
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


@app.route("/epic")
def epic_status():
    """Connectivity status page for epic_client.py's EpicClient —
    deliberately decoupled from the NW GMSA `client` this app otherwise
    uses throughout (see CLAUDE.md's "Epic FHIR connectivity" section):
    this route never touches `g.client`/the FhirClient session, only
    EpicClient's own environment-variable config. Shows what's
    configured (base URL, client id, scope, JWT kid — never the private
    key itself) and, only when explicitly requested via ?test=1 (not on
    every page load — this hits Epic's real server), the result of
    EpicClient.verify_connection(). There's still no registered sandbox
    app as of this route's introduction, so "not configured"/a failed
    test are the expected results until EPIC_CLIENT_ID/
    EPIC_PRIVATE_KEY_PATH/EPIC_SCOPE are actually set."""
    try:
        base_url, client_id, _private_key_pem, kid, scope, _verify_ssl = EpicClient.config()
        configured = True
        config_error = None
    except RuntimeError as e:
        configured = False
        config_error = str(e)
        base_url = os.environ.get("EPIC_FHIR_BASE_URL") or EPIC_FHIR_BASE_URL_DEFAULT
        client_id = os.environ.get("EPIC_CLIENT_ID")
        kid = os.environ.get("EPIC_JWT_KID")
        scope = os.environ.get("EPIC_SCOPE")

    tested = request.args.get("test") == "1"
    test_result = None
    test_error = None
    if tested:
        try:
            fhir_version, software_name = EpicClient.verify_connection()
            test_result = {"fhir_version": fhir_version, "software_name": software_name}
        except Exception as e:
            test_error = str(e)

    return render_template(
        "epic.html",
        configured=configured, config_error=config_error,
        base_url=base_url, client_id=client_id, kid=kid, scope=scope,
        tested=tested, test_result=test_result, test_error=test_error,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
