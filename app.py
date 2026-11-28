import os
import re
import json
import secrets
import difflib
import threading
import time
from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, Response, abort, redirect, session, g, url_for
from werkzeug.local import LocalProxy
import requests
import pandas as pd
import plotly.express as px
from dotenv import load_dotenv
from fhir_client import FhirClient
import fhir_validator
import hl7v2_mapping
from iris_client import IrisClient
from pdf_report import monthly_report_pdf_bytes, quality_report_pdf_bytes, validation_report_pdf_bytes
from epic_client import EpicClient, EPIC_FHIR_BASE_URL_DEFAULT, KNOWN_SANDBOX_TEST_PATIENTS

# Loads .env into the process environment (FHIR_BASE_URL, ESB_*, EPIC_*, ...)
# — every env var this app reads is read lazily inside a function/__init__,
# not at import time, so it's enough for this to run before any request
# comes in; it doesn't need to precede the imports above. Does not override
# variables already set in the real environment (e.g. by a deployment's own
# process manager), same as load_dotenv()'s own default behaviour.
load_dotenv()

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

#: Password required to confirm a per-patient clear-down
#: (/patient/<id>/clear-down) when client.is_production() — the extra
#: gate that lets that screen be enabled on production at all.
#: Hardcoded for now; move to an env var before this is relied on.
CLEAR_DOWN_PASSWORD = "Grosmont321admin"


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
    # Testing menu (/testing*) is for test systems only — 404 (not 403)
    # on production, so it doesn't even advertise that it exists there.
    if request.path.startswith("/testing") and fhir_client.is_production():
        abort(404)


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


#: Labels for epic_client.py's group_family_history() generation keys —
#: only the common few generations get a specific name (matching the
#: generations FAMILY_RELATIONSHIP_INFO's codes actually reach: parents/
#: grandparents/great-grandparents, children/grandchildren), anything
#: further out falls back to a generic "N generations up/down" label
#: rather than needing this table extended for a code that's never come
#: up in practice.
FAMILY_GENERATION_LABELS = {
    3: "Great-grandparents", 2: "Grandparents", 1: "Parents", 0: "Same generation",
    -1: "Children", -2: "Grandchildren",
}


def family_generation_label(generation):
    """Human label for one of group_family_history()'s top-level keys —
    an int generation (positive = ancestors, negative = descendants, 0 =
    same generation) or the literal string "other" for a relationship
    code with no known generation (see FAMILY_RELATIONSHIP_INFO)."""
    if generation == "other":
        return "Other relatives"
    if generation in FAMILY_GENERATION_LABELS:
        return FAMILY_GENERATION_LABELS[generation]
    if generation > 0:
        return f"Ancestors ({generation} generations up)"
    return f"Descendants ({-generation} generations down)"


def family_generation_sort_key(generation):
    """Sort key ordering group_family_history()'s generation keys oldest
    ancestor first, down through descendants, with "other" last —
    pedigree reading order, not the arbitrary dict/insertion order
    group_family_history() itself builds."""
    if generation == "other":
        return (1, 0)
    return (0, -generation)


def family_side_sort_key(side):
    """Sort key for group_family_history()'s per-generation side keys —
    maternal before paternal before "unspecified", rather than whatever
    order Python happened to insert them in."""
    return ({"maternal": 0, "paternal": 1}.get(side, 2), side)


def family_history_sections(family_member_histories):
    """epic_client.py's group_family_history() output, reshaped into a
    flat, display-ready ordering for the Pathology Explorer's Family
    history section: a list of (generation_label, [(side, entries), ...])
    tuples, generations oldest-ancestor-first then "Other relatives"
    last, sides maternal/paternal/unspecified within each."""
    groups = EpicClient.group_family_history(family_member_histories)
    sections = []
    for generation in sorted(groups.keys(), key=family_generation_sort_key):
        sides = groups[generation]
        side_entries = [
            (side, sides[side]) for side in sorted(sides.keys(), key=family_side_sort_key)
        ]
        sections.append((family_generation_label(generation), side_entries))
    return sections


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
app.jinja_env.filters["specimen_tracking_numbers"] = FhirClient.specimen_tracking_numbers
app.jinja_env.filters["specimen_container_identifiers"] = FhirClient.specimen_container_identifiers
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
app.jinja_env.filters["all_identifiers"] = all_identifiers


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
    document_references = []
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
    # Fetched separately from the block above, deliberately: whether
    # this server's DocumentReference support even exists is unconfirmed
    # (no IG-documented category to check like lab_orders_for_patient()/
    # lab_reports_for_patient() have), so a failure here shouldn't take
    # down the whole page — it just shows no documents.
    try:
        document_references = client.documents_for_patient(patient_id)
    except Exception:
        document_references = []
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
        specimens=specimens, document_references=document_references,
        error=error, is_production=client.is_production(),
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
        "specimen_container_id": request.form.get("specimen_container_id", ""),
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
            "specimen_container_id",
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
        specimen_tracking_numbers = FhirClient.as_value_list(form_values["specimen_tracking_number"])
        specimen_container_ids = FhirClient.as_value_list(form_values["specimen_container_id"])

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
                    specimen_tracking_number=specimen_tracking_numbers,
                    specimen_container_id=specimen_container_ids,
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

    When client.is_production(), the POST additionally requires
    CLEAR_DOWN_PASSWORD (a password field on the confirm form) — checked
    here on the POST itself, not just shown in the UI, so a direct POST
    without it still can't delete anything. A wrong password re-renders
    the confirm page with an error rather than deleting.
    """
    password_required = client.is_production()

    if request.method == "POST" and password_required and not secrets.compare_digest(
        request.form.get("password", "").encode(), CLEAR_DOWN_PASSWORD.encode()
    ):
        password_error = "Incorrect password — nothing was deleted."
    else:
        password_error = None

    if request.method == "POST" and not password_error:
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
        password_required=password_required, password_error=password_error,
    ), 403 if password_error else 200


@app.route("/testing")
def testing_menu():
    """Testing menu — test systems only (see _load_client())."""
    return render_template("testing_menu.html")


#: Background FHIR validation runs for /testing/send-message, keyed by a
#: random token (same in-memory, no-expiry pattern as _session_clients;
#: each validator run takes tens of seconds, too long to block a request
#: on — behind IIS it would time out). Only the session that started a
#: job can see it.
_validation_jobs = {}

#: The "Validate individual resources" panel (and the per-issue "Validate
#: <entry>" buttons). Replaced by the automatic follow-up validation
#: (fhir_validator.follow_up_targets()); the panel, its template block and
#: action=validate_entries are kept so this can be turned back on.
INDIVIDUAL_VALIDATION_ENABLED = False


def _validation_result(key, label, resource, choice, bundle_rule="", auto=False, represents=None):
    """One entry of a validation job's `results` (not yet validated)."""
    ig_key, profile = fhir_validator.parse_choice(choice)
    if bundle_rule:
        # The rule's profile is an NW one; the NW package also pulls in
        # Genomics Reporting (a dependency), so either choice resolves.
        ig_key = "nw-gmsa"
    rule = fhir_validator.BUNDLE_RULES.get(bundle_rule)
    result = {
        "key": key, "label": label, "resource": resource, "choice": choice,
        "ig": ig_key, "ig_label": fhir_validator.IGS[ig_key]["label"], "profile": profile,
        "bundle_rule": bundle_rule or "",
        "bundle_rule_text": " ".join(["-bundle", rule["rule"], rule["profile"]]) if rule else None,
        "auto": auto, "represents": represents or [],
    }
    return _apply_validation_backend([result])[0]


def _is_message_bundle(resource):
    return (isinstance(resource, dict) and resource.get("resourceType") == "Bundle"
            and resource.get("type") == "message")


def _fhir_validation_context(text):
    """Validate-panel context for a FHIR preview: the validation targets
    (whole resource, plus each Bundle entry) with their profile choices,
    whether it's a message Bundle (the only thing that can be sent), and
    anything stopping the validator from running at all."""
    try:
        resource = json.loads(text)
    except ValueError as e:
        return {"parse_error": f"Not valid JSON — {e}", "targets": [], "is_message_bundle": False}
    if not isinstance(resource, dict) or not resource.get("resourceType"):
        return {"parse_error": "Not a FHIR resource (no resourceType).", "targets": [],
                "is_message_bundle": False}
    targets = fhir_validator.validation_targets(resource)
    choices = {}
    for t in targets:
        if t["resource_type"] not in choices:
            choices[t["resource_type"]] = fhir_validator.profile_choices(t["resource_type"])
        t["choices"] = choices[t["resource_type"]]
    return {
        "parse_error": None,
        "targets": targets,
        "is_message_bundle": _is_message_bundle(resource),
        "bundle_rule_choices": fhir_validator.bundle_rule_choices(),
        "default_bundle_rule": (fhir_validator.default_bundle_rule(resource)
                                if _is_message_bundle(resource) else ""),
        "message_event": (fhir_validator.message_event_code(resource)
                          if _is_message_bundle(resource) else None),
        "tx_options": fhir_validator.terminology_options(),
        "setup_problems": fhir_validator.setup_problems(),
        "backend": fhir_validator.backend(),
        "esb_validate_url": FhirClient.esb_validate_url(),
    }


def _apply_validation_backend(results):
    """With the ESB backend, the ESB picks the profile / -bundle rule, so
    each result is labelled with what it's expected to use rather than the
    (ignored) choice made on the page."""
    if fhir_validator.backend() != fhir_validator.BACKEND_ESB:
        return results
    for r in results:
        r["profile"], r["bundle_rule_text"] = fhir_validator.esb_validation_plan(r["resource"])
        r["ig_label"] = fhir_validator.ESB_IG_LABEL
        r["bundle_rule"] = ""
    return results


def _validation_tx(value):
    if fhir_validator.backend() == fhir_validator.BACKEND_ESB:
        return "set by the ESB (FHIR.Validate)"
    return value or fhir_validator.TX_FHIR_ORG


def _hl7v2_validation_context(text):
    """Validate-panel context for an HL7 v2 preview: validated by the
    ESB's $validate, which converts it to FHIR first — so only with the
    ESB backend."""
    problems = []
    if fhir_validator.backend() != fhir_validator.BACKEND_ESB:
        problems.append("HL7 v2 messages can only be validated by the ESB (it converts them to FHIR first) — "
                        "FHIR_VALIDATION_BACKEND is set to local.")
    return {
        "parse_error": None, "is_v2": True, "is_message_bundle": False,
        "targets": [{"key": "all", "label": "HL7 v2 message", "resource_type": "HL7 v2",
                     "default": "", "choices": []}],
        "message_event": hl7v2_mapping.message_event(text),
        "backend": fhir_validator.backend(), "esb_validate_url": FhirClient.esb_validate_url(),
        "setup_problems": problems,
    }


def _run_validation_job(job):
    try:
        if job.get("v2"):
            # The ESB converts the v2 message; the report is about that
            # FHIR Message, located back in the v2 message at the end.
            first = job["results"][0]
            if not first.get("validated"):
                fhir_validator.validate_hl7v2(first, job["v2"])
                first["validated"] = True
            bundle = first.get("resource") or None
        else:
            try:
                source = json.loads(job["message"])
            except ValueError:
                source = None
            bundle = source if isinstance(source, dict) and source.get("resourceType") == "Bundle" else None
        # A run that adds individual resources to an earlier report
        # carries that report's results over already validated.
        to_run = [r for r in job["results"] if not r.get("validated")]
        if to_run:
            fhir_validator.validate(to_run, job["tx"])
        for r in to_run:
            r["validated"] = True
        fhir_validator.annotate_profile_mismatches(job["results"], bundle)
        if job.get("follow_up"):
            # Entries named in "Unable to find a profile match" are
            # validated on their own straight away, against their type's
            # default profile (one Observation stands in for the rest).
            follow_up = [_validation_result(t["key"], t["label"], t["resource"], t["choice"],
                                            auto=True, represents=t["represents"])
                         for t in fhir_validator.follow_up_targets(job["results"], bundle)]
            if follow_up:
                job["stage"] = "follow_up"
                job["results"].extend(follow_up)
                fhir_validator.validate(follow_up, job["tx"])
                for r in follow_up:
                    r["validated"] = True
                fhir_validator.annotate_profile_mismatches(job["results"], bundle)
        if job.get("v2"):
            hl7v2_mapping.annotate(job["results"], bundle, job["v2"],
                                   fhir_validator._profile_mismatch, fhir_validator._find_entry)
    except Exception as e:
        job["error"] = str(e)
    job["status"] = "done"
    job["finished"] = time.time()


def _validation_report_options():
    """(include_information, ignore patterns) from the report's query
    string — shared by the page and the PDF so they match. Ignored issues
    are the ESB's job now (FHIR.Validate's IgnoredIssues), so there's no
    longer a box to change them: always fhir_validator.DEFAULT_IGNORED_ISSUES
    (a no-op on the ESB's output; still applied to the local validator's)."""
    return request.args.get("info") == "1", None


def _owned_validation_job(job_id):
    job = _validation_jobs.get(job_id or "")
    return job if job and job["sid"] == session.get("sid") else None


@app.route("/testing/send-message/report.pdf")
def testing_validation_report_pdf():
    """The validation report as a PDF — same options (?info=, ?ignore=)
    as the page's report section."""
    job = _owned_validation_job(request.args.get("validation"))
    if not job or job["status"] != "done":
        abort(404)
    include_information, ignore = _validation_report_options()
    report = fhir_validator.build_report(job["results"], include_information, ignore)
    pdf_bytes = validation_report_pdf_bytes(report, job)
    stem = os.path.splitext(job.get("filename") or "")[0] or "fhir"
    filename = f"{re.sub(r'[^A-Za-z0-9._-]+', '_', stem)}-validation-report.pdf"
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.route("/testing/send-message", methods=["GET", "POST"])
def testing_send_message():
    """Upload (or paste) an HL7 v2 or FHIR message, preview it, validate
    it (FHIR only) and/or submit it to the test ESB — FHIR to
    $process-message, HL7 v2 POSTed over HTTPS to the order filler
    (FhirClient.send_hl7v2_http()). action=upload detects the type
    and shows the preview; action=validate starts a background validator
    run (fhir_validator.py — local validator_cli.jar, never the server's
    $validate) and redirects to ?validation=<id>, which shows the results
    and refreshes itself while running; action=send actually sends (the
    only branch with an external side effect). A FHIR resource can be
    validated whatever it is, but only a message Bundle can be sent.
    Nothing is written to the FHIR server."""
    process_message_url, hl7v2_url = FhirClient.test_message_targets()
    ctx = {
        "process_message_url": process_message_url,
        "hl7v2_target": hl7v2_url,
        "message": None, "message_type": None, "filename": None,
        "error": None, "result": None, "validation": None, "validation_job": None,
        "now_ts": time.time(), "report": None, "report_pdf_url": None, "entry_targets": None,
        "pick_error": False, "individual_validation": INDIVIDUAL_VALIDATION_ENABLED,
        "v2_message": None, "v2_segments": None,
    }
    job_id = request.args.get("validation")
    if request.method == "GET" and job_id:
        job = _owned_validation_job(job_id)
        if not job:
            ctx["error"] = "That validation run isn't available (the app may have restarted)."
        else:
            ctx.update(message=job["message"], message_type=job.get("message_type", "fhir"), filename=job["filename"],
                       validation_job=job, selected_tx=job["tx"],
                       selected={r["key"]: r["choice"] for r in job["results"]},
                       selected_bundle_rule=next((r.get("bundle_rule", "") for r in job["results"]
                                                  if r["key"] == "all"), None))
    if request.method == "POST":
        action = request.form.get("action")
        if action == "upload":
            upload = request.files.get("message_file")
            text = ""
            if upload and upload.filename:
                ctx["filename"] = upload.filename
                raw = upload.read()
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
            else:
                text = request.form.get("message_text", "")
            if not text.strip():
                ctx["error"] = "Choose a file or paste a message."
            else:
                ctx["message"] = text
                ctx["message_type"] = FhirClient.detect_message_type(text)
                if not ctx["message_type"]:
                    ctx["error"] = ("Couldn't tell what kind of message this is — expected a FHIR JSON "
                                    "resource (starting with '{') or an HL7 v2 message (starting with 'MSH').")
        elif action == "validate" and (FhirClient.detect_message_type(request.form.get("message", ""))
                                       or request.form.get("message_type")) == "hl7v2":
            text = request.form.get("message", "")
            ctx.update(message=text, message_type="hl7v2", filename=request.form.get("filename"))
            problems = _hl7v2_validation_context(text)["setup_problems"]
            if problems:
                ctx["error"] = " ".join(problems)
            else:
                event = hl7v2_mapping.message_event(text)
                job_id = secrets.token_urlsafe(16)
                _validation_jobs[job_id] = job = {
                    "sid": session.get("sid"), "status": "running", "error": None,
                    "message": text, "message_type": "hl7v2", "v2": hl7v2_mapping.normalise(text),
                    "filename": ctx["filename"], "tx": _validation_tx(None),
                    "results": [{
                        "key": "all", "resource": {}, "choice": "", "ig": fhir_validator.DEFAULT_IG,
                        "ig_label": fhir_validator.ESB_IG_LABEL, "profile": None, "bundle_rule": "",
                        "bundle_rule_text": None, "auto": False, "represents": [],
                        "label": f"HL7 v2 message{f' ({event})' if event else ''} — converted to FHIR",
                    }],
                    "started": time.time(), "finished": None, "follow_up": True, "stage": "bundle",
                }
                threading.Thread(target=_run_validation_job, args=(job,), daemon=True).start()
                return redirect(url_for("testing_send_message", validation=job_id))
        elif action == "validate":
            text = request.form.get("message", "")
            ctx.update(message=text, message_type="fhir", filename=request.form.get("filename"))
            try:
                resource = json.loads(text)
            except ValueError as e:
                resource = None
                ctx["error"] = f"Can't validate — message is not valid JSON: {e}"
            if resource is not None:
                if not isinstance(resource, dict) or not resource.get("resourceType"):
                    ctx["error"] = "Can't validate — not a FHIR resource (no resourceType)."
                else:
                    # First run: the whole resource only — individual
                    # Bundle entries are offered once its report is in.
                    wanted = {"all"}
                    results = []
                    for t in fhir_validator.validation_targets(resource):
                        if t["key"] not in wanted:
                            continue
                        choice = request.form.get(f"profile_{t['key']}") or t["default"]
                        bundle_rule = ""
                        if t["key"] == "all" and _is_message_bundle(resource):
                            bundle_rule = request.form.get("bundle_rule", "")
                            if bundle_rule not in fhir_validator.BUNDLE_RULES:
                                bundle_rule = ""
                        results.append(_validation_result(t["key"], t["label"], t["resource"], choice, bundle_rule))
                    if not results:
                        ctx["error"] = "Tick at least one resource to validate."
                    else:
                        tx = _validation_tx(request.form.get("tx"))
                        job_id = secrets.token_urlsafe(16)
                        job = {
                            "sid": session.get("sid"), "status": "running", "error": None,
                            "message": text, "filename": ctx["filename"], "tx": tx,
                            "results": results, "started": time.time(), "finished": None,
                            "follow_up": True, "stage": "bundle",
                        }
                        _validation_jobs[job_id] = job
                        threading.Thread(target=_run_validation_job, args=(job,), daemon=True).start()
                        return redirect(url_for("testing_send_message", validation=job_id))
        elif action == "validate_entries":
            parent_id = request.form.get("validation")
            parent = _owned_validation_job(parent_id)
            if not parent or parent["status"] != "done":
                ctx["error"] = "That validation report isn't available any more (the app may have restarted)."
            else:
                # The editor's current text (posted alongside by the page's
                # JS), so edits made since the Bundle report count.
                text = request.form.get("message") or parent["message"]
                try:
                    bundle = json.loads(text)
                except ValueError as e:
                    bundle = None
                    ctx.update(message=text, message_type="fhir", filename=parent["filename"],
                               error=f"Can't validate — message is not valid JSON: {e}")
                if bundle is not None and not isinstance(bundle, dict):
                    ctx.update(message=text, message_type="fhir", filename=parent["filename"],
                               error="Can't validate — not a FHIR resource.")
            if parent and parent["status"] == "done" and isinstance(bundle, dict):
                all_targets = fhir_validator.validation_targets(bundle)
                targets = {t["key"]: t for t in all_targets if t["key"] != "all"}
                # Either one "Validate this resource" button (quick =
                # "<entry key>|<ig>|<profile>") or the ticked entries.
                picks = []
                if request.form.get("quick"):
                    key, _, choice = request.form["quick"].partition("|")
                    picks.append((key, choice))
                else:
                    picks = [(key, request.form.get(f"profile_{key}") or "")
                             for key in request.form.getlist("validate_entry")]
                new = []
                for key, choice in picks:
                    t = targets.get(key)
                    if not t:
                        continue
                    new.append(_validation_result(key, t["label"], t["resource"], choice or t["default"]))
                if not new:
                    return redirect(url_for("testing_send_message", validation=parent_id,
                                            pick_error=1, _anchor="validate-entries"))
                rerun = {(r["key"], r["choice"]) for r in new}
                # Earlier results whose resource has since been edited are
                # kept but flagged, so the report shows they're out of date.
                current = {t["key"]: fhir_validator.canonical(t["resource"]) for t in all_targets}
                kept = [{**r, "stale": r.get("stale") or current.get(r["key"]) != fhir_validator.canonical(r["resource"])}
                        for r in parent["results"] if (r["key"], r["choice"]) not in rerun]
                job_id = secrets.token_urlsafe(16)
                _validation_jobs[job_id] = job = {
                    "sid": session.get("sid"), "status": "running", "error": None,
                    "message": text, "filename": parent["filename"],
                    "tx": _validation_tx(request.form.get("tx") or parent["tx"]),
                    "results": kept + new, "started": time.time(), "finished": None,
                }
                threading.Thread(target=_run_validation_job, args=(job,), daemon=True).start()
                return redirect(url_for("testing_send_message", validation=job_id))
        elif action == "send":
            text = request.form.get("message", "")
            # Re-detect from the (possibly edited) text, falling back to the
            # type picked at upload so a half-edited message stays editable.
            message_type = FhirClient.detect_message_type(text) or request.form.get("message_type")
            ctx.update(message=text, message_type=message_type, filename=request.form.get("filename"))
            try:
                if message_type == "fhir":
                    try:
                        bundle = json.loads(text)
                    except ValueError as e:
                        raise ValueError(f"Message is not valid JSON — {e}") from None
                    if not _is_message_bundle(bundle):
                        raise ValueError("Only a FHIR message Bundle (resourceType Bundle, type "
                                         "\"message\") can be sent — this can only be validated.")
                    status, body = FhirClient.send_fhir_message_to_esb(bundle)
                    hl7v2 = FhirClient.extract_hl7v2_message(body)
                    ctx["result"] = {
                        "target": process_message_url, "status": status,
                        "hl7v2": hl7v2.replace("\r\n", "\n").replace("\r", "\n") if hl7v2 else None,
                        "body": json.dumps(body, indent=2) if isinstance(body, (dict, list)) else body,
                    }
                elif message_type == "hl7v2":
                    status, ack = FhirClient.send_hl7v2_http(text)
                    ctx["result"] = {
                        "target": ctx["hl7v2_target"], "status": status,
                        "ack_code": FhirClient.hl7v2_ack_code(ack),
                        "body": ack.replace("\r\n", "\n").replace("\r", "\n"),
                    }
                else:
                    ctx["error"] = "Unknown message type."
            except Exception as e:
                ctx["error"] = f"Send failed: {e}"
    if ctx["message"] and ctx["message_type"] == "fhir":
        ctx["validation"] = _fhir_validation_context(ctx["message"])
    elif ctx["message"] and ctx["message_type"] == "hl7v2":
        ctx["validation"] = _hl7v2_validation_context(ctx["message"])
    if ctx["validation_job"] and ctx["validation_job"].get("v2"):
        ctx["v2_segments"] = hl7v2_mapping.segments(ctx["validation_job"]["v2"])
        ctx["v2_message"] = "\n".join(ctx["v2_segments"])
    if ctx["validation_job"] and ctx["validation_job"]["status"] == "done":
        include_information, ignore = _validation_report_options()
        ctx["report"] = fhir_validator.build_report(ctx["validation_job"]["results"], include_information, ignore)
        pdf_args = {"validation": job_id}
        if include_information:
            pdf_args["info"] = "1"
        ctx["report_pdf_url"] = url_for("testing_validation_report_pdf", **pdf_args)
        # Individual Bundle entries can be validated once the report is in:
        # suggested ones (referenced by "Unable to find a profile match")
        # first, with the profile the reference wanted.
        targets = [t for t in (ctx["validation"] or {}).get("targets", []) if t["key"] != "all"]
        if targets:
            sugs = fhir_validator.suggestions(ctx["validation_job"]["results"])
            suggested = {}
            for sug in sugs:
                suggested.setdefault(sug["key"], sug)
            for t in targets:
                t["suggested"] = suggested.get(t["key"])
                if t["suggested"] and t["resource_type"] not in fhir_validator.TYPE_DEFAULT_PROFILES:
                    t["default"] = t["suggested"]["choice"]
            targets.sort(key=lambda t: 0 if t["suggested"] else 1)
            ctx["entry_targets"] = targets
        ctx["pick_error"] = request.args.get("pick_error") == "1"
    ctx["validation_job_id"] = job_id if ctx["validation_job"] else None
    # Keep the validate panel's picks across a re-render (a validation
    # run's own picks, else whatever was just posted, else defaults:
    # only the whole resource ticked).
    ctx.setdefault("selected", {k[len("profile_"):]: v for k, v in request.form.items()
                                if k.startswith("profile_")})
    ctx.setdefault("selected_tx", request.form.get("tx"))
    ctx.setdefault("selected_bundle_rule", request.form.get("bundle_rule"))
    if ctx["validation_job"]:
        ctx["checked"] = {r["key"] for r in ctx["validation_job"]["results"]}
    elif request.form.getlist("validate_target"):
        ctx["checked"] = set(request.form.getlist("validate_target"))
    else:
        ctx["checked"] = {"all"}
    return render_template("testing_send_message.html", **ctx)


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
    Active filler-order work items — ServiceRequests with
    intent=filler-order (client.active_filler_orders()), bounded by
    `authored` to [start, end]. This was briefly Task-based
    (active_filler_tasks(), Task.owner) but moved back to ServiceRequest
    because of problems with Task on this deployment.

    The owner picker (FhirClient.TASK_OWNER_OPTIONS — K1S6S Liverpool GLH
    Starlims by default, or the DLIMS Omics DSS Device) now scopes
    ServiceRequest.performer, the ServiceRequest counterpart of
    Task.owner; ?owner= empty means any performer.
    """
    end = request.args.get("end") or date.today().isoformat()
    start = request.args.get("start") or (date.today() - timedelta(days=30)).isoformat()
    owner_ods = request.args.get("owner", FhirClient.DEFAULT_TASK_OWNER_ODS_CODE)
    selected_status = request.args.get("status", FhirClient.DEFAULT_SERVICE_REQUEST_STATUS)
    sort = request.args.get("sort", "")

    error = None
    owner_org = None
    rows = []
    try:
        owner_org = client.resolve_task_owner(owner_ods) if owner_ods else None
        if owner_org or not owner_ods:
            performer_reference = f"{owner_org['resourceType']}/{owner_org['id']}" if owner_org else None
            for o in client.active_filler_orders(start, end, performer_reference=performer_reference,
                                                 status=selected_status):
                patient = client.patient_for(o)
                rows.append({
                    "id": o.get("id"),
                    "patient_id": patient.get("id") if patient else None,
                    "patient_name": human_name(patient) if patient else "Unknown",
                    "status": o.get("status") or "—",
                    "test": FhirClient.test_directory_code(o.get("code")) or code_text(o.get("code")) or "—",
                    "intent": o.get("intent") or "—",
                    "ordered": o.get("authoredOn") or "—",
                    "requested_by": client.requester_display(o),
                    "performer": client.performer_display(o),
                    "filler_id": FhirClient.filler_identifier(o) or "—",
                    "group_identifier": FhirClient.requisition_identifier(o) or "—",
                })
    except Exception as e:
        error = str(e)

    if sort in ("ordered_asc", "ordered_desc"):
        rows.sort(key=lambda r: r["ordered"] or "", reverse=(sort == "ordered_desc"))

    if owner_org and owner_org.get("resourceType") == "Device":
        owner_name = FhirClient.device_display(owner_org)
    else:
        owner_name = owner_org.get("name") if owner_org else None
    owner_display_name = (_org_display_name(owner_name, owner_ods) if owner_name else owner_ods) if owner_org else None

    return render_template(
        "work_orders.html", rows=rows, sort=sort,
        start=start, end=end, owner_ods=owner_ods, owner_display_name=owner_display_name,
        owner_found=bool(owner_org) or not owner_ods, owner_options=FhirClient.TASK_OWNER_OPTIONS,
        statuses=FhirClient.SERVICE_REQUEST_STATUS_VALUES, selected_status=selected_status,
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

    The "Reports by ordering provider" map uses organisations_geocoded()
    directly, with each report's ordering organisation (_report_ordering()).
    """
    return organisations_geocoded(
        (client.order_organisation_resource(order), client.order_organisation(order))
        for order in orders
    )


def organisations_geocoded(org_items):
    """orders_by_organisation_geocoded()'s grouping/geocoding, over
    (Organization resource or None, fallback display name) pairs — one
    pair per counted order/report."""
    orgs = {}
    for org_resource, fallback_name in org_items:
        name = (org_resource.get("name") if org_resource else None) or fallback_name or "Unknown"
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


#: Pages fetched by the daily stats screens' order and report searches —
#: up to 10,000 of each at _count=100 (raised from the default 10 /
#: 1,000). Hitting it shows a "may be incomplete" warning (_stats_fetch()).
STATS_MAX_PAGES = 100


def _stats_fetch(start, end):
    """orders_in_range() + reports_in_range() for the /stats screens, at
    STATS_MAX_PAGES — returns (orders, reports, truncated), `truncated`
    listing which of "orders"/"reports" stopped at the cap with pages left
    (client.last_search_truncated, read straight after each search, since
    any later search overwrites it)."""
    truncated = []
    orders = client.orders_in_range(start, end, max_pages=STATS_MAX_PAGES)
    if client.last_search_truncated:
        truncated.append("orders")
    reports = client.reports_in_range(start, end, max_pages=STATS_MAX_PAGES)
    if client.last_search_truncated:
        truncated.append("reports")
    return orders, reports, truncated


def _canonical_org(org):
    """One record per organisation on the /stats screens: an
    Organization reached by any route (ODS code off the order/report, or
    a resolved requester reference to this FHIR server's own
    Organization) is swapped for client.organisation_by_ods() on its ODS
    code — the ODS trust list's record for an NHS trust. Without this the
    same trust showed twice in the monthly report: once from the ODS list
    ("Manchester University NHS Foundation Trust") and once from the
    server's own Organization ("MANCHESTER UNIVERSITY NHS FOUNDATION
    TRUST"), since rows are grouped by name. Returns `org` unchanged if
    it has no ODS code."""
    ods = client.organisation_ods_code(org) if org else None
    return (client.organisation_by_ods(ods) if ods else None) or org


def _org_name(org):
    """An Organization's name for display/grouping, with an all-capitals
    name (ODS convention, e.g. a server Organization backfilled by
    scripts/fix_organization_names.py) title-cased the same way the ODS
    trust list's names are, so two sources can't differ only by case."""
    name = (org or {}).get("name")
    return FhirClient._ods_name_case(name) if name and name.isupper() else name


def _order_ordering(order):
    """(requesting Organization resource or None, requesting organisation
    name) for one ServiceRequest on the /stats screens — read off the
    order itself (client.order_ordering_organisation(): requester
    identifier, encounter or placer-number assigner ODS code), since
    orders_in_range() no longer includes the requester chain. Only when
    the order carries no ODS code at all does this fall back to resolving
    the requester reference (order_organisation_resource() — one GET per
    distinct requester, cached), so an order with a literal-only requester
    still gets a name."""
    org = _canonical_org(client.order_ordering_organisation(order) or client.order_organisation_resource(order))
    name = _org_name(org) or client.order_organisation(order) or "Unknown"
    return org, name


def _report_ordering(report, orders_index):
    """(matched order or None, ordering Organization resource or None,
    ordering organisation name) for one DiagnosticReport on the /stats
    screens — without fetching the report's ServiceRequest (that
    `_include` 413'd on production; see reports_in_range()).

    The order is only one already in `orders_index`
    (_orders_index_for_reports(): the in-range orders plus a batched fetch
    of the ServiceRequests of reports with no ODS code of their own —
    client.match_report_order()). The organisation comes off the report
    itself (client.report_ordering_organisation()), falling back to the
    matched order's requesting organisation (_order_ordering())."""
    order = client.match_report_order(report, orders_index)
    org = _canonical_org(client.report_ordering_organisation(report))
    if org:
        name = _org_name(org) or "Unknown"
    elif order:
        org, name = _order_ordering(order)
    else:
        name = "Unknown"
    if not org and _patient_inferred_key(report) in orders_index:
        # No order link at all: the trust all the patient's orders came
        # from (_infer_ordering_from_patient_orders()).
        org = _canonical_org(client.organisation_by_ods(orders_index[_patient_inferred_key(report)]))
        name = _org_name(org) or name
    return order, org, name


def _orders_index_for_reports(reports, orders=()):
    """index_orders() over `orders` (e.g. orders_in_range()'s, possibly
    none) plus the originating ServiceRequests of every report whose order
    isn't already in `orders` and that needs it — no ordering ODS code of
    its own, *or* no test code the categoriser recognises — fetched in
    batches by client.service_requests_for_reports() (`_id=a,b,c,...`, no
    `_include`). Most production reports carry the trust's ODS code only
    on their ServiceRequest (requester.identifier), so without this most
    of them came out "Unknown"; and a report with no Test Directory /
    NW local / Histotrac code of its own (only SNOMED "Genetic report", or
    only an iGene IGEAP code) needs its order's R/M code — report_category()
    prefers that over any LOINC/IGEAP code, so the order is fetched
    whenever the report itself lacks one, not only when the report can't
    be categorised at all."""
    index = FhirClient.index_orders(orders)
    # "No ordering organisation" means none that resolves — a report whose
    # own code is a sender typo ("ROA") needs its order's requester too.
    missing = [
        r for r in reports
        if (not client.report_ordering_organisation(r) or not FhirClient._test_code_and_display(r.get("code"))[0])
        and not client.match_report_order(r, index)
    ]
    if missing:
        for key, order in FhirClient.index_orders(client.service_requests_for_reports(missing)).items():
            index.setdefault(key, order)
    _infer_ordering_from_patient_orders(reports, index)
    return index


def _patient_inferred_key(report):
    return f"DiagnosticReport/{report.get('id')}#patient-ods"


def _infer_ordering_from_patient_orders(reports, index):
    """For reports with no link to their order at all — no ordering
    organisation of their own, no `basedOn` order in `index` (e.g. Shire
    karyotype reports, which carry no basedOn) — record an ordering ODS
    code in `index` under _patient_inferred_key() when *every* one of the
    patient's orders authored on or before the report's date points at
    the same trust. Several trusts, or none, leaves the report Unknown.
    Only the organisation is inferred, never the order itself (so a
    report's category never comes from a guessed order)."""
    unlinked = [
        r for r in reports
        if (r.get("subject") or {}).get("reference")
        and not client.report_ordering_organisation(r)
        and not client.match_report_order(r, index)
    ]
    if not unlinked:
        return
    by_patient = client.service_requests_for_patients(r["subject"]["reference"] for r in unlinked)
    for r in unlinked:
        report_date = (r.get("issued") or r.get("effectiveDateTime") or "")[:10]
        candidates = [
            o for o in by_patient.get(r["subject"]["reference"], [])
            if not report_date or (o.get("authoredOn") or "")[:10] <= report_date
        ]
        codes = {client.order_ordering_ods(o) for o in candidates}
        if len(codes) == 1 and None not in codes:
            index[_patient_inferred_key(r)] = codes.pop()


def _reports_by_order_id(reports, orders_index):
    """{order id: most recently issued report} over reports whose
    originating order is in `orders_index` (match_report_order()) — the
    stats drill-downs' pairing of orders with their reports."""
    by_order = {}
    for r in reports:
        order = client.match_report_order(r, orders_index)
        if not order:
            continue
        oid = order.get("id")
        existing = by_order.get(oid)
        issued = r.get("issued") or r.get("effectiveDateTime") or ""
        existing_issued = (existing.get("issued") or existing.get("effectiveDateTime") or "") if existing else ""
        if existing is None or issued > existing_issued:
            by_order[oid] = r
    return by_order


def _report_only_row(r, order):
    """A /stats drill-down row for a report whose order wasn't in the
    fetched order range (or didn't match) — order-only fields "—"."""
    patient = client.patient_for(r)
    return {
        "order_id": order.get("id") if order else None,
        "report_id": r.get("id"),
        "patient_id": patient.get("id") if patient else None,
        "patient_name": human_name(patient) if patient else "Unknown",
        "requester": client.requesting_clinician_display(order) if order else "—",
        "test": FhirClient.test_directory_code(r.get("code")) or "—",
        "status": r.get("status") or "—",
        "order_date": (order.get("authoredOn") if order else None) or "—",
        "collected_date": "—",
        "received_date": "—",
        "reported_date": (r.get("issued") or r.get("effectiveDateTime") or "")[:10] or "—",
        "placer_id": (FhirClient.placer_identifier(order) if order else None) or "—",
        "igene_id": client.igene_report_identifier(order or {}, r) or "—",
        "conclusion": "; ".join(code_text(cc) for cc in (r.get("conclusionCode") or [])) or "—",
        "spell_id": (client.hospital_spell_identifier(order) if order else None) or client.hospital_spell_identifier(r) or "—",
    }


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
    truncated = []
    try:
        orders, reports, truncated = _stats_fetch(start, end)
        order_rows = []
        order_orgs = []
        for o in orders:
            patient = client.patient_for(o)
            order_org_resource, order_org_name = _order_ordering(o)
            order_orgs.append((order_org_resource, order_org_name))
            order_rows.append({
                "date": (o.get("authoredOn") or "")[:10] or "Unknown",
                "organisation": order_org_name,
                "indication": client.order_indication(o),
                "ics": client.organisation_ics(order_org_resource) or "Unknown",
                "country": client.patient_country(patient) or "Unknown",
            })

        org_geo = organisations_geocoded(order_orgs)
        order_map_points = [g for g in org_geo if g["lat"] is not None and g["lon"] is not None]
        order_map_unmapped_count = sum(g["count"] for g in org_geo if g["lat"] is None)

        order_ics_map_html, order_ics_map_unmatched_count, order_ics_map_unmatched_names = ics_choropleth_html(group_count(order_rows, "ics"))

        orders_index = _orders_index_for_reports(reports, orders)
        report_rows = []
        report_orgs = []
        for r in reports:
            patient = client.patient_for(r)
            # Who *ordered* the test — separate from report_organisation()
            # (the *performing* lab, from DiagnosticReport.performer). Read
            # off the report itself, not its ServiceRequest (see
            # _report_ordering()).
            ordering_order, ordering_org_resource, ordering_name = _report_ordering(r, orders_index)
            report_orgs.append((ordering_org_resource, ordering_name))
            report_rows.append({
                "date": (r.get("issued") or r.get("effectiveDateTime") or "")[:10] or "Unknown",
                "organisation": client.report_organisation(r) or "Unknown",
                "ordering_provider": ordering_name,
                "indication": client.report_indication(r, ordering_order),
                "ics": client.organisation_ics(ordering_org_resource) or "Unknown",
                "country": client.patient_country(patient) or "Unknown",
            })

        report_org_geo = organisations_geocoded(report_orgs)
        report_map_points = [g for g in report_org_geo if g["lat"] is not None and g["lon"] is not None]
        report_map_unmapped_count = sum(g["count"] for g in report_org_geo if g["lat"] is None)

        report_ics_map_html, report_ics_map_unmatched_count, report_ics_map_unmatched_names = ics_choropleth_html(group_count(report_rows, "ics"))
    except Exception as e:
        error = str(e)

    return render_template(
        "stats.html", start=start, end=end, error=error, truncated=truncated, max_pages=STATS_MAX_PAGES,
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
    truncated = []
    try:
        orders, reports, truncated = _stats_fetch(start, end)

        orders_index = _orders_index_for_reports(reports, orders)
        report_by_order_id = _reports_by_order_id(reports, orders_index)

        matched_order_ids = set()
        order_test_rows, report_test_rows = [], []
        for o in orders:
            if _order_ordering(o)[1] != org:
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
            ordering_order, _, ordering_provider = _report_ordering(r, orders_index)
            if ordering_provider != org:
                continue
            report_test_rows.append({"test": FhirClient.test_directory_code(r.get("code")) or "—"})
            if ordering_order and ordering_order.get("id") in matched_order_ids:
                continue  # already emitted above, via its order
            row = _report_only_row(r, ordering_order)
            if org == "Unknown":
                row["requester_code"] = _requester_code(ordering_order)
            rows.append(row)

        order_by_test = group_count(order_test_rows, "test")
        report_by_test = group_count(report_test_rows, "test")
    except Exception as e:
        error = str(e)

    return render_template(
        "stats_organisation.html", org=org, start=start, end=end, error=error,
        truncated=truncated, max_pages=STATS_MAX_PAGES,
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
    truncated = []
    try:
        orders, reports, truncated = _stats_fetch(start, end)

        orders_index = _orders_index_for_reports(reports, orders)
        report_by_order_id = _reports_by_order_id(reports, orders_index)

        matched_order_ids = set()
        order_test_rows, report_test_rows = [], []
        for o in orders:
            order_ics = client.organisation_ics(_order_ordering(o)[0]) or "Unknown"
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
            ordering_order, ordering_org_resource, _ = _report_ordering(r, orders_index)
            report_ics = client.organisation_ics(ordering_org_resource) or "Unknown"
            if report_ics != ics:
                continue
            report_test_rows.append({"test": FhirClient.test_directory_code(r.get("code")) or "—"})
            if ordering_order and ordering_order.get("id") in matched_order_ids:
                continue  # already emitted above, via its order
            rows.append(_report_only_row(r, ordering_order))

        order_by_test = group_count(order_test_rows, "test")
        report_by_test = group_count(report_test_rows, "test")
    except Exception as e:
        error = str(e)

    return render_template(
        "stats_ics.html", ics=ics, start=start, end=end, error=error,
        truncated=truncated, max_pages=STATS_MAX_PAGES,
        rows=rows, order_by_test=order_by_test, report_by_test=report_by_test,
    )


def _requesting_code_provided(report, order, org, ordering_ods):
    """(code, name, source) describing what a report says about who ordered
    it — for the monthly report's "Unknown ICS" breakdown, so the codes
    that couldn't be placed (or the lack of any) can be chased up with the
    sender. `code` is the ODS code the report/order carries, else the
    order requester's identifier value, else its literal reference, else
    None; `source` says where it came from, or why there's nothing."""
    if ordering_ods:
        return ordering_ods, _org_name(org), "ODS code — organisation couldn't be placed in an ICS"
    if org:
        return None, _org_name(org), "Organisation with no ODS code"
    if not order:
        if not report.get("basedOn"):
            return None, None, "No ordering organisation on the report, and no basedOn order reference"
        return None, None, "No ordering organisation on the report; its order wasn't found"
    requester = order.get("requester") or {}
    if not requester:
        return None, None, "Order has no requester"
    ident = requester.get("identifier") or {}
    if ident.get("value"):
        system = ident.get("system")
        return ident["value"], requester.get("display"), f"Order requester identifier ({system or 'no system'})"
    if requester.get("reference"):
        return requester["reference"], requester.get("display"), "Order requester reference (couldn't be resolved)"
    return None, requester.get("display"), "Order requester has only a display name"


#: Data quality issues the monthly report lists (_report_data_quality_issues()):
#: key -> (label, what to do about it).
DATA_QUALITY_ISSUES = {
    "ods_typo": ("ODS code typo",
                 "A letter O sent for a zero. The ESB now corrects it "
                 "(RIE.FHIR.Library.NormaliseODSCode()); records already stored keep it, and this report corrects them."),
    "local_code": ("Local code instead of an ODS code",
                   "Map it to the trust's ODS code — ODSCodeCorrections lookup table on the ESB, "
                   "FhirClient.LOCAL_ODS_ALIASES here — or ask the sender to send the ODS code."),
    "country_as_ods": ("Country code sent as an ODS code",
                       "A country outside the UK has no ODS code: it should be its ISO 3166-1 alpha-3 code "
                       "under urn:iso:std:iso:3166 (the ESB now does this)."),
    "shire_no_order_placed": ("Shire report with no order link — placed from the patient's orders",
                              "Shire ORU messages carry no ORC, placer number or ordering facility, so nothing links "
                              "the report to its order; the trust was inferred (all the patient's orders came from it)."),
    "shire_no_order_unknown": ("Shire report with no order link — ordering organisation unknown",
                               "As above, but the patient's orders came from more than one trust (or there are none). "
                               "Needs Shire to send the order (placer/HODS order number) or ordering facility."),
}

#: Example reports kept per data quality issue row.
DATA_QUALITY_EXAMPLES = 10


def _raw_organisation_codes(report, order):
    """(where, system, value) for every organisation code the report and
    its order carry, *as sent* — before FhirClient.normalise_ods_code()."""
    codes = []
    assigner = (((report.get("encounter") or {}).get("identifier") or {}).get("assigner") or {}).get("identifier") or {}
    if assigner.get("value"):
        codes.append(("report encounter assigner", assigner.get("system"), assigner["value"]))
    for ref in report.get("basedOn") or []:
        assigner = (((ref.get("identifier") or {}).get("assigner") or {}).get("identifier")) or {}
        if assigner.get("value"):
            codes.append(("report basedOn placer number assigner", assigner.get("system"), assigner["value"]))
    if order:
        ident = (order.get("requester") or {}).get("identifier") or {}
        if ident.get("value"):
            codes.append(("order requester", ident.get("system"), ident["value"]))
        for ident in order.get("identifier") or []:
            assigner = ((ident.get("assigner") or {}).get("identifier")) or {}
            if assigner.get("value"):
                codes.append(("order identifier assigner", assigner.get("system"), assigner["value"]))
    return codes


def _report_data_quality_issues(report, order, org, orders_index):
    """{(issue key, detail): set of where} for one report — see
    DATA_QUALITY_ISSUES. `detail` is e.g. "ROA → R0A"."""
    issues = {}
    for where, system, value in _raw_organisation_codes(report, order):
        if system not in (None, "", FhirClient.ODS_ORGANIZATION_CODE_SYSTEM):
            continue
        upper = value.strip().upper()
        if upper in FhirClient.LOCAL_ODS_ALIASES:
            key = ("local_code", f"{value} → {FhirClient.LOCAL_ODS_ALIASES[upper]}")
        elif upper in FhirClient.ISO3166_ALPHA3_COUNTRIES and upper not in FhirClient.ods_trusts():
            key = ("country_as_ods", f"{value} ({FhirClient.ISO3166_ALPHA3_COUNTRIES[upper]})")
        elif FhirClient.normalise_ods_code(value) != value:
            key = ("ods_typo", f"{value} → {FhirClient.normalise_ods_code(value)}")
        else:
            continue
        issues.setdefault(key, set()).add(where)
    is_shire = any(i.get("system") == "https://fhir.nwgenomics.nhs.uk/Shire/ReportIdentifier"
                   for i in report.get("identifier") or [])
    if is_shire and not report.get("basedOn") and not order:
        placed = bool(org) and _patient_inferred_key(report) in orders_index
        issues.setdefault(("shire_no_order_placed" if placed else "shire_no_order_unknown", ""), set()).add(
            "DiagnosticReport (no basedOn, encounter or ordering organisation)")
    return issues


def short_icb_name(icb_name):
    """"NHS Greater Manchester Integrated Care Board" -> "Greater
    Manchester" — just for column/row labels on the monthly report."""
    n = re.sub(r"^NHS\s+", "", icb_name or "")
    return re.sub(r"\s+Integrated Care Board$", "", n)


def _month_range(start_month, end_month):
    """Every "YYYY-MM" from start_month to end_month inclusive."""
    y, m = map(int, start_month.split("-"))
    ey, em = map(int, end_month.split("-"))
    months = []
    while (y, m) <= (ey, em):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


#: Upper bound on how many months /stats/monthly-reports will query in one
#: go — each month is its own reports_in_range() search.
MONTHLY_REPORTS_MAX_MONTHS = 24

#: Example reports listed per row of the monthly report's "Unknown ICS"
#: breakdown (newest meta.lastUpdated first) — to find the original
#: messages they came from.
UNKNOWN_ICS_EXAMPLES = 20
#: Each month is searched in windows of this many days, each with its own
#: page cap — one search for a whole busy month hit the cap on production.
MONTHLY_REPORTS_WINDOW_DAYS = 7
#: Pages fetched per window — up to 10,000 reports a week at _count=100
#: (raised from 20 / 2,000). A window that still had pages left flags its
#: month as incomplete (client.last_search_truncated).
MONTHLY_REPORTS_MAX_PAGES = 100


def _code_signature(codeable_concept, missing):
    """Short text describing what a CodeableConcept actually carries —
    every coding as "system|code (display)", else its `.text`, else
    `missing` — for the monthly report's "Unclassified reports" table."""
    cc = codeable_concept or {}
    codings = [
        f"{c.get('system') or '(no system)'}|{c.get('code') or '(no code)'}"
        + (f" ({c['display']})" if c.get("display") else "")
        for c in cc.get("coding", [])
    ]
    if codings:
        return "; ".join(codings)
    return f"text only: {cc['text']}" if cc.get("text") else missing


#: Which trusts' reports are *actually delivered* to them electronically,
#: by ODS code — "live" (delivered today) or "nearing" (nearing go-live).
#: Not derivable from the FHIR data, so maintained here by hand: update it
#: as trusts go live. Every other trust counts as not delivered.
TRUST_DELIVERY_STATUS = {
    "R0A": "live",     # Manchester University NHS Foundation Trust (MFT)
    "RBS": "nearing",  # Alder Hey Children's NHS Foundation Trust
    "REP": "nearing",  # Liverpool Women's NHS Foundation Trust
}
TRUST_DELIVERY_LABELS = {"live": "Live", "nearing": "Nearing go-live", None: "Not live"}
#: How many trusts the "recommended targets" list shows.
DELIVERY_TARGETS_SHOWN = 10


def _delivery_summary(trust_totals, total):
    """The monthly report's "Reports actually delivered" section from
    {(trust label, ODS code or None, ICS): report count} — counts and % of
    `total` per TRUST_DELIVERY_STATUS status, and the top
    DELIVERY_TARGETS_SHOWN trusts by volume that are neither live nor
    already nearing go-live, as recommended targets. Trusts with no ODS
    code ("Unknown") are counted but can't be targets."""
    def pct(n):
        return round(100 * n / total, 1) if total else 0.0

    statuses = []
    for status in ("live", "nearing", None):
        trusts = [(label, n) for (label, ods, _), n in trust_totals.items()
                  if TRUST_DELIVERY_STATUS.get(ods) == status and n]
        n = sum(c for _, c in trusts)
        statuses.append({
            "label": TRUST_DELIVERY_LABELS[status], "reports": n, "pct": pct(n),
            # Named only for live/nearing — "Not live" is everyone else.
            "trusts": sorted(label for label, _ in trusts) if status else [],
        })

    candidates = sorted(
        ((label, ods, ics, n) for (label, ods, ics), n in trust_totals.items()
         if ods and n and not TRUST_DELIVERY_STATUS.get(ods)),
        key=lambda t: (-t[3], t[0]),
    )[:DELIVERY_TARGETS_SHOWN]
    targets = [
        {"name": label, "ics": short_icb_name(ics) if ics != "Unknown" else "Unknown", "reports": n, "pct": pct(n)}
        for label, ods, ics, n in candidates
    ]
    return {"total": total, "statuses": statuses, "targets": targets}


#: How many distinct code combinations the "Unclassified reports" table lists.
UNCLASSIFIED_SIGNATURES_SHOWN = 25


def _build_monthly_report():
    """
    Everything the monthly report shows, as the template context dict —
    shared by monthly_reports() (HTML) and monthly_reports_pdf(), so the
    two can never disagree. Reads the same query params either way.

    Genomic test reports per calendar month (default: the last full month
    only; the current month can be picked too, counted up to today), by ICS and report category, filterable by NHS region and
    category, with each ICS expandable into its NHS trusts.

    The report search has no `_include` (an `_include=DiagnosticReport:
    based-on` chain made a month's search 413 on production) and runs in
    week-sized windows (MONTHLY_REPORTS_WINDOW_DAYS). The trust is the
    ordering organisation's ODS code — off the report itself where it
    carries one (client.report_ordering_ods()), otherwise off its
    ServiceRequest, fetched in id batches without includes
    (_orders_index_for_reports() / _report_ordering()). Its ICS and NHS
    region come from ODS's trust -> ICB mapping
    (FhirClient.trust_geography()). Category is FhirClient.report_category() on the
    report's own code.

    Queried one week-sized window at a time rather than one big range, so
    each search stays bounded — see "413s on unfiltered system-wide
    searches" in CLAUDE.md.
    """
    today = date.today()
    this_month = today.strftime("%Y-%m")
    # Default to just the last full calendar month — one month's search,
    # not a year's worth; widen with start_month/end_month when needed.
    last_full_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    end_month = request.args.get("end_month") or last_full_month
    start_month = request.args.get("start_month") or last_full_month
    selected_region = request.args.get("region", "")
    selected_category = request.args.get("category", "")

    error = None
    months = []
    try:
        months = _month_range(start_month, min(end_month, this_month))
    except ValueError:
        error = "Months must be in YYYY-MM format."
    if len(months) > MONTHLY_REPORTS_MAX_MONTHS:
        error = f"Pick at most {MONTHLY_REPORTS_MAX_MONTHS} months."
        months = []

    category_labels = dict(FhirClient.REPORT_CATEGORIES)
    # ics -> month -> category -> count; ics -> trust -> month -> category -> count
    ics_counts, trust_counts = {}, {}
    ics_region_names = {}  # ICB name -> NHS region name, for ICBs seen in the data
    trust_ods = {}  # (ICS, trust label) -> ODS code, for the delivery section
    # ICS -> (report code, order code) -> count, for unclassified reports —
    # shows which codes need mapping (see _code_signature()).
    unclassified_by_ics = {}
    # (code, name, source) -> month -> category -> count, for reports
    # whose ICS is "Unknown" (see _requesting_code_provided()).
    unknown_codes = {}
    # (code, name, source) -> [report, ...] — examples to trace back to the
    # original message (UNKNOWN_ICS_EXAMPLES newest kept per row).
    unknown_examples = {}
    # Reports placed by _infer_ordering_from_patient_orders() (no order
    # link; all the patient's orders from one trust) — counted, shown on
    # the page, so an inferred attribution is never silent.
    patient_inferred_reports = 0
    # (issue key, detail) -> {"where": set, "months": {month: n}, "examples": [report, ...]}
    data_quality = {}
    truncated_months = []
    selected_leaf_keys = FhirClient.category_leaf_keys(selected_category) if selected_category else None
    try:
        seen = set()
        for month in months:
            y, m = map(int, month.split("-"))
            month_start = date(y, m, 1)
            month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            month_end = min(month_end, today)
            # Week-sized windows rather than one search per month — a busy
            # month has more reports than one search's page cap, and each
            # window gets its own cap.
            reports = []
            window_start = month_start
            while window_start <= month_end:
                window_end = min(window_start + timedelta(days=MONTHLY_REPORTS_WINDOW_DAYS - 1), month_end)
                window = client.reports_in_range(window_start.isoformat(), window_end.isoformat(),
                                                 max_pages=MONTHLY_REPORTS_MAX_PAGES, with_includes=False)
                if client.last_search_truncated and month not in truncated_months:
                    truncated_months.append(month)
                reports.extend(window)
                window_start = window_end + timedelta(days=1)
            reports = [r for r in reports if r.get("id") not in seen and not seen.add(r.get("id"))]
            orders_index = _orders_index_for_reports(reports)
            for r in reports:
                order, org, _ = _report_ordering(r, orders_index)
                if org and not order and _patient_inferred_key(r) in orders_index:
                    patient_inferred_reports += 1
                for key, where in _report_data_quality_issues(r, order, org, orders_index).items():
                    entry = data_quality.setdefault(key, {"where": set(), "months": {}, "examples": []})
                    entry["where"] |= where
                    entry["months"][month] = entry["months"].get(month, 0) + 1
                    entry["examples"].append(r)
                ordering_ods = client.organisation_ods_code(org) if org else None
                geo = FhirClient.organisation_geography(
                    ordering_ods, name=_org_name(org), postcode=client.organisation_postcode(org)) if org else None
                ics = geo["icb_name"] if geo else "Unknown"
                if geo:
                    ics_region_names[ics] = geo["region_name"]
                trust_name = _org_name(org)
                trust = _org_display_name(trust_name or ordering_ods, ordering_ods) if (trust_name or ordering_ods) else "Unknown"
                trust_ods[(ics, trust)] = ordering_ods
                category = FhirClient.report_category(r.get("code"), order.get("code") if order else None)
                if category == "unclassified":
                    signature = (
                        _code_signature(r.get("code"), "(no code)"),
                        _code_signature(order.get("code"), "(no code)") if order else "(no linked order found)",
                    )
                    sigs = unclassified_by_ics.setdefault(ics, {})
                    sigs[signature] = sigs.get(signature, 0) + 1
                by_cat = ics_counts.setdefault(ics, {}).setdefault(month, {})
                by_cat[category] = by_cat.get(category, 0) + 1
                if ics == "Unknown":
                    key = _requesting_code_provided(r, order, org, ordering_ods)
                    by_cat = unknown_codes.setdefault(key, {}).setdefault(month, {})
                    by_cat[category] = by_cat.get(category, 0) + 1
                    if not selected_leaf_keys or category in selected_leaf_keys:
                        unknown_examples.setdefault(key, []).append(r)
                by_cat = trust_counts.setdefault(ics, {}).setdefault(trust, {}).setdefault(month, {})
                by_cat[category] = by_cat.get(category, 0) + 1
    except Exception as e:
        error = str(e)

    def count(month_cats, month):
        cats = month_cats.get(month, {})
        if selected_leaf_keys:
            return sum(cats.get(k, 0) for k in selected_leaf_keys)
        return sum(cats.values())

    def category_tree_rows(leaf_months, keep_empty):
        """Category x month rows in REPORT_CATEGORY_TREE order — a row per
        category, followed by its sub-category rows (`child: True`); a
        parent's months are its children's sums. `leaf_months(key)` gives
        one leaf key's per-month counts. Only top-level rows should be
        summed for a total. `keep_empty`: also list categories with no
        reports (except No test performed/Unclassified, which only show
        when they have some)."""
        rows = []
        for key, label, children in FhirClient.REPORT_CATEGORY_TREE:
            child_rows = [
                {"key": ck, "label": cl, "months": leaf_months(ck), "child": True}
                for ck, cl in children
            ]
            months_ = ([sum(c["months"][i] for c in child_rows) for i in range(len(months))]
                       if child_rows else leaf_months(key))
            total = sum(months_)
            if not total and (not keep_empty or key in ("unclassified", "no_test")):
                continue
            rows.append({"key": key, "label": label, "months": months_, "total": total, "child": False})
            for c in child_rows:
                c["total"] = sum(c["months"])
                if c["total"] or keep_empty:
                    rows.append(c)
        return rows

    # Every current ICB in the selected region (or all regions) is listed,
    # zero-filled, so a region view shows its whole ICS list — from ODS's
    # trust list (FhirClient.icbs_by_region()), not a hardcoded one, since
    # ICBs merge. An ICB with data that isn't current (a trust that's since
    # closed) is still listed under its region; "Unknown" (no ODS trust
    # mapping) only appears in the all-regions view.
    icbs_by_region = FhirClient.icbs_by_region()
    for region, icbs in icbs_by_region.items():
        for icb in icbs:
            ics_region_names.setdefault(icb, region)
    regions = list(FhirClient.NHS_REGION_NAMES.values()) + [FhirClient.OUTSIDE_ENGLAND_REGION]
    if selected_region:
        ics_list = list(icbs_by_region.get(selected_region, []))
        ics_list += sorted(i for i in ics_counts if i not in ics_list and ics_region_names.get(i) == selected_region)
    else:
        ics_list = [icb for icbs in icbs_by_region.values() for icb in icbs]
        ics_list += sorted(i for i in ics_counts if i not in ics_list)
    if not icbs_by_region and not error:
        error = "Couldn't download the NHS ODS trust list, so ICS couldn't be worked out — showing only ICSs with data."

    def category_breakdown(month_cats):
        """Category x month rows for one ICS or trust — the "Categories"
        popup. Always every category (the category filter doesn't apply:
        this *is* the breakdown); categories and sub-categories with no
        reports are left out."""
        return category_tree_rows(
            lambda key: [month_cats.get(mo, {}).get(key, 0) for mo in months], keep_empty=False)

    ics_rows = []
    for ics in ics_list:
        month_cats = ics_counts.get(ics, {})
        trusts = []
        for trust, trust_month_cats in sorted(trust_counts.get(ics, {}).items()):
            trust_months = [count(trust_month_cats, mo) for mo in months]
            if sum(trust_months):
                trusts.append({"name": trust, "months": trust_months, "total": sum(trust_months),
                               "categories": category_breakdown(trust_month_cats)})
        trusts.sort(key=lambda t: -t["total"])
        ics_months = [count(month_cats, mo) for mo in months]
        ics_rows.append({
            "name": ics, "label": short_icb_name(ics), "region": ics_region_names.get(ics) or "—",
            "months": ics_months, "total": sum(ics_months), "trusts": trusts,
            "categories": category_breakdown(month_cats),
        })

    month_totals = [sum(r["months"][i] for r in ics_rows) for i in range(len(months))]

    # "Reports actually delivered" — same ICS list (region filter) and
    # same counting (category filter) as the ICS table, so its total
    # matches that table's.
    trust_totals = {}
    for ics in ics_list:
        for trust, trust_month_cats in trust_counts.get(ics, {}).items():
            key = (trust, trust_ods.get((ics, trust)), ics)
            trust_totals[key] = trust_totals.get(key, 0) + sum(count(trust_month_cats, mo) for mo in months)
    delivery = _delivery_summary(trust_totals, sum(month_totals))

    # "Unknown" ICS: what requesting organisation code each report gave
    # (category filter applied, like the ICS table, so the totals match
    # its Unknown row). Only in the all-regions view, where Unknown is.
    unknown_code_rows = []
    if not selected_region:
        for (code, name, source), month_cats in unknown_codes.items():
            code_months = [count(month_cats, mo) for mo in months]
            if sum(code_months):
                examples = sorted(unknown_examples.get((code, name, source), []),
                                  key=lambda rep: (rep.get("meta") or {}).get("lastUpdated") or "", reverse=True)
                unknown_code_rows.append({
                    "code": code, "name": name, "source": source,
                    "months": code_months, "total": sum(code_months),
                    "examples": [{
                        "id": rep.get("id"),
                        "last_updated": (rep.get("meta") or {}).get("lastUpdated"),
                        "issued": rep.get("issued"),
                        "identifiers": all_identifiers(rep),
                    } for rep in examples[:UNKNOWN_ICS_EXAMPLES]],
                })
        unknown_code_rows.sort(key=lambda r: (-r["total"], r["code"] or "", r["source"]))

    # Data quality issues — every report in the months shown (region and
    # category filters don't apply: these are about the data, not counts).
    data_quality_rows = []
    order_of_issues = list(DATA_QUALITY_ISSUES)
    for (key, detail), entry in sorted(data_quality.items(), key=lambda kv: (order_of_issues.index(kv[0][0]), kv[0][1])):
        label, fix = DATA_QUALITY_ISSUES[key]
        months_ = [entry["months"].get(mo, 0) for mo in months]
        examples = sorted(entry["examples"], key=lambda rep: (rep.get("meta") or {}).get("lastUpdated") or "",
                          reverse=True)[:DATA_QUALITY_EXAMPLES]
        data_quality_rows.append({
            "issue": label, "detail": detail, "fix": fix, "where": sorted(entry["where"]),
            "months": months_, "total": sum(months_),
            "examples": [{"id": rep.get("id"), "last_updated": (rep.get("meta") or {}).get("lastUpdated"),
                          "identifiers": all_identifiers(rep)} for rep in examples],
        })

    # Unclassified reports' code combinations over the ICS list above
    # (region filter applied), most common first.
    unclassified_signatures = {}
    for ics in ics_list:
        for signature, n in unclassified_by_ics.get(ics, {}).items():
            unclassified_signatures[signature] = unclassified_signatures.get(signature, 0) + n

    # Category x month for the ICS list above (region filter applied,
    # category filter not — this table *is* the category breakdown).
    category_rows = category_tree_rows(
        lambda key: [sum(ics_counts.get(ics, {}).get(mo, {}).get(key, 0) for ics in ics_list) for mo in months],
        keep_empty=True)

    return dict(
        error=error,
        start_month=start_month, end_month=end_month, months=months,
        regions=regions, selected_region=selected_region,
        categories=FhirClient.REPORT_CATEGORIES, selected_category=selected_category,
        selected_category_label=category_labels.get(selected_category),
        ics_rows=ics_rows, month_totals=month_totals, category_rows=category_rows,
        category_month_totals=[sum(r["months"][i] for r in category_rows if not r["child"]) for i in range(len(months))],
        truncated_months=truncated_months, max_pages=MONTHLY_REPORTS_MAX_PAGES,
        unclassified_signatures=unclassified_signatures, unclassified_total=sum(unclassified_signatures.values()),
        unclassified_shown=UNCLASSIFIED_SIGNATURES_SHOWN, delivery=delivery,
        unknown_code_rows=unknown_code_rows, patient_inferred_reports=patient_inferred_reports,
        data_quality_rows=data_quality_rows,
        unknown_code_month_totals=[sum(r["months"][i] for r in unknown_code_rows) for i in range(len(months))],
    )


#: The last monthly report each login built, keyed by (session id, query
#: string), so "Download as PDF" straight after viewing the page doesn't
#: re-run every search. Kept MONTHLY_REPORT_CACHE_SECONDS; same
#: in-process dict approach as _session_clients/_order_load_cache.
_monthly_report_cache = {}
MONTHLY_REPORT_CACHE_SECONDS = 600


def _monthly_report_context():
    """_build_monthly_report() for the current request, reusing this
    login's result for the same query string if it's still fresh."""
    key = (session.get("sid"), tuple(sorted(request.args.items(multi=True))))
    now = time.time()
    for k in [k for k, (built, _) in _monthly_report_cache.items() if now - built > MONTHLY_REPORT_CACHE_SECONDS]:
        _monthly_report_cache.pop(k, None)
    cached = _monthly_report_cache.get(key)
    if cached:
        return cached[1]
    context = _build_monthly_report()
    _monthly_report_cache[key] = (now, context)
    return context


@app.route("/stats/monthly-reports")
def monthly_reports():
    """The monthly report page — see _build_monthly_report()."""
    return render_template("monthly_reports.html", **_monthly_report_context())


#: How many example reports per Unknown row the diagnostics download
#: probes by default (?per_row=, at most UNKNOWN_ICS_EXAMPLES) — each costs
#: a handful of read-only FHIR searches.
UNKNOWN_DIAGNOSTICS_PER_ROW = 5

#: DiagnosticReport / ServiceRequest elements kept in the diagnostics
#: download — the linking/identifying ones. Everything else (conclusion,
#: presentedForm, notes, ...) is dropped, and patient references are
#: reduced to their FHIR id (see _redact_subject()).
_DIAGNOSTIC_REPORT_KEEP = ("resourceType", "id", "meta", "status", "category", "code", "identifier", "basedOn",
                           "encounter", "performer", "resultsInterpreter", "specimen", "issued",
                           "effectiveDateTime", "effectivePeriod", "result")
_SERVICE_REQUEST_KEEP = ("resourceType", "id", "meta", "status", "intent", "category", "code", "identifier",
                         "basedOn", "requisition", "requester", "performer", "encounter", "specimen", "authoredOn")


def _redact_subject(ref):
    """A patient reference with only its literal reference kept — no NHS
    number or name leaves the server in the diagnostics download."""
    if not isinstance(ref, dict):
        return ref
    return {"reference": ref.get("reference"), "identifier": "(redacted)" if ref.get("identifier") else None,
            "display": "(redacted)" if ref.get("display") else None}


def _diagnostic_copy(resource, keep):
    copy = {k: resource[k] for k in keep if k in resource}
    if "subject" in resource:
        copy["subject"] = _redact_subject(resource["subject"])
    return copy


def _diagnostic_search(resource_type, params):
    """One read-only search, recorded as {"request", "ok", "count",
    "service_requests"|"error"} — errors are captured, not raised, since
    a failing lookup is exactly what the download is for."""
    entry = {"request": f"{resource_type}?" + "&".join(f"{k}={v}" for k, v in params.items())}
    try:
        bundle = client._get(resource_type, params=params)
    except Exception as e:
        entry.update(ok=False, error=str(e)[:2000])
        return entry
    found = [e.get("resource") or {} for e in bundle.get("entry", [])]
    entry.update(ok=True, total=bundle.get("total"),
                 other_entries=[r.get("resourceType") for r in found if r.get("resourceType") != resource_type],
                 service_requests=[{**_diagnostic_copy(r, _SERVICE_REQUEST_KEEP),
                                    "ordering_ods_found_by_app": client.order_ordering_ods(r)}
                                   for r in found if r.get("resourceType") == resource_type])
    return entry


def _report_link_diagnostics(report):
    """Every lookup that could link `report` to its ServiceRequest, and
    what each one returned on this server."""
    out = {
        "report": _diagnostic_copy(report, _DIAGNOSTIC_REPORT_KEEP),
        "ordering_ods_on_report_found_by_app": client.report_ordering_ods(report),
        "basedOn_lookups": [],
    }
    for ref in report.get("basedOn") or []:
        literal = ref.get("reference") or ""
        lookup = {"basedOn": ref}
        if "ServiceRequest/" in literal:
            sr_id = literal.split("ServiceRequest/")[-1].split("/")[0]
            try:
                sr = client._get(f"ServiceRequest/{sr_id}")
                lookup["read"] = {"request": f"ServiceRequest/{sr_id}", "ok": True,
                                  "service_request": {**_diagnostic_copy(sr, _SERVICE_REQUEST_KEEP),
                                                      "ordering_ods_found_by_app": client.order_ordering_ods(sr)}}
            except Exception as e:
                lookup["read"] = {"request": f"ServiceRequest/{sr_id}", "ok": False, "error": str(e)[:2000]}
            # The search the monthly report itself uses (batched there).
            lookup["search_by_id"] = _diagnostic_search("ServiceRequest", {"_id": sr_id, "_count": 10})
        ident = ref.get("identifier") or {}
        if ident.get("value"):
            value = f"{ident['system']}|{ident['value']}" if ident.get("system") else ident["value"]
            lookup["search_by_identifier"] = _diagnostic_search(
                "ServiceRequest", {"identifier": value.replace(",", "\\,"), "_count": 10})
        out["basedOn_lookups"].append(lookup)
    # Orders sharing an identifier value with the report (accession /
    # filler number) — commas escaped, a bare comma means OR in FHIR search.
    values = [i["value"].replace(",", "\\,") for i in report.get("identifier") or [] if i.get("value")]
    if values:
        out["orders_sharing_report_identifier"] = _diagnostic_search(
            "ServiceRequest", {"identifier": ",".join(values), "_count": 20})
    subject = (report.get("subject") or {}).get("reference")
    if subject:
        out["patient_orders"] = _diagnostic_search("ServiceRequest", {"subject": subject, "_count": 20})
    return out


@app.route("/stats/monthly-reports/unknown-diagnostics.json")
def monthly_reports_unknown_diagnostics():
    """A JSON download for working out why reports land in the monthly
    report's Unknown ICS, for when the server can't be queried from
    elsewhere: for up to ?per_row= (default UNKNOWN_DIAGNOSTICS_PER_ROW)
    example reports from each Unknown row (same query string as the page),
    the report's linking elements and the result of every lookup that
    could find its ServiceRequest (_report_link_diagnostics()). Read-only.
    Patient references are cut down to their FHIR id; clinical text
    (conclusion, notes, attachments) isn't included."""
    context = _monthly_report_context()
    try:
        per_row = max(1, min(int(request.args.get("per_row", UNKNOWN_DIAGNOSTICS_PER_ROW)), UNKNOWN_ICS_EXAMPLES))
    except ValueError:
        per_row = UNKNOWN_DIAGNOSTICS_PER_ROW
    rows = []
    for row in context.get("unknown_code_rows") or []:
        examples = []
        for ex in row["examples"][:per_row]:
            try:
                report = client._get(f"DiagnosticReport/{ex['id']}")
            except Exception as e:
                examples.append({"report_id": ex["id"], "error": str(e)[:2000]})
                continue
            examples.append(_report_link_diagnostics(report))
        rows.append({"code_provided": row["code"], "name_given": row["name"], "where_it_came_from": row["source"],
                     "reports": row["total"], "examples": examples})
    body = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "fhir_base_url": client.base_url,
        "query": dict(request.args),
        "months": context.get("months"),
        "note": "Read-only diagnostics. Patient identifiers/names redacted; clinical text not included.",
        "unknown_rows": rows,
    }
    months = context.get("months") or []
    span = f"{months[0]}-to-{months[-1]}" if months else "report"
    return Response(json.dumps(body, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="unknown-ics-diagnostics-{span}.json"'})


@app.route("/stats/monthly-reports/pdf")
def monthly_reports_pdf():
    """The monthly report as a PDF download — the same data as the page
    for the same query string (the page's "Download as PDF" link carries
    it), via pdf_report.monthly_report_pdf_bytes(). Since the PDF can't
    expand rows or open popups, it lists every ICS's trusts and each
    ICS's category breakdown in full."""
    context = _monthly_report_context()
    pdf_bytes = monthly_report_pdf_bytes(context)
    months = context.get("months") or []
    span = f"{months[0]}-to-{months[-1]}" if months else "report"
    filename = f"monthly-reports-{span}.pdf"
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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


@app.route("/patient/document/<document_id>")
def patient_document(document_id):
    """Streams one attachment off a NW GMSA DocumentReference — this
    side's counterpart to /report/<report_id>/pdf and (on the Epic side)
    /pathology/document/<document_id>. document_id alone is enough (no
    patient_id needed) since get_document_reference() is a direct Read."""
    index = int(request.args.get("index", 0))
    try:
        document = client.get_document_reference(document_id)
    except Exception as e:
        return f"Could not load document: {e}", 502

    contents = document.get("content") or []
    if index >= len(contents):
        abort(404, description="This document has no attachment at that index.")
    attachment = contents[index].get("attachment") or {}

    try:
        data, content_type = client.fetch_attachment_bytes(attachment)
    except Exception as e:
        return f"Could not fetch attachment: {e}", 502
    if data is None:
        abort(404, description="Attachment had neither inline data nor a URL.")

    filename = attachment.get("title") or f"{document_id}-{index}"
    return Response(
        data, mimetype=content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.route("/pathology", methods=["GET"])
def pathology_index():
    """Landing/search page for the Pathology Explorer — epic_client.py's
    EpicClient counterpart to the NW Genomics side's '/' search page.
    Deliberately separate from `g.client`/the NW GMSA session (see
    CLAUDE.md's "Epic FHIR connectivity" section) — this page and
    everything under /pathology only ever talks to Epic."""
    return render_template("pathology_index.html", test_patients=KNOWN_SANDBOX_TEST_PATIENTS)


@app.route("/pathology/search", methods=["GET"])
def pathology_search():
    family = request.args.get("family", "").strip()
    given = request.args.get("given", "").strip()
    birthdate = request.args.get("birthdate", "").strip()
    identifier = request.args.get("identifier", "").strip()
    patient_id = request.args.get("patient_id", "").strip()
    error = None
    patients = []
    try:
        if patient_id:
            patients = EpicClient.search_patients(patient_id=patient_id)
        elif identifier:
            patients = EpicClient.search_patients(identifier=identifier)
        elif family or given or birthdate:
            patients = EpicClient.search_patients(
                family=family or None, given=given or None, birthdate=birthdate or None,
            )
    except Exception as e:
        error = str(e)
    return render_template(
        "pathology_index.html", patients=patients, error=error,
        searched_family=family, searched_given=given, searched_birthdate=birthdate,
        searched_identifier=identifier, searched_id=patient_id,
        test_patients=KNOWN_SANDBOX_TEST_PATIENTS,
    )


@app.route("/pathology/patient/<patient_id>")
def pathology_patient(patient_id):
    """Patient-scoped Pathology Explorer view — every DiagnosticReport
    Epic has for this patient (category=None, not just
    DIAGNOSTIC_REPORT_GENETICS_CATEGORY — this screen deliberately
    covers pathology as well as genomics, unlike the NW Genomics side),
    each report's resolvable Observation results, Condition (problem
    list), ServiceRequest (orders), DocumentReference (clinical
    documents), and FamilyMemberHistory (family history/pedigree)."""
    error = None
    patient = None
    reports = []
    report_observations = {}
    conditions = []
    service_requests = []
    document_references = []
    family_history = []
    try:
        patient = EpicClient.get_patient(patient_id)
        reports = EpicClient.diagnostic_reports_for_patient(patient_id, category=None)
        for report in reports:
            if report.get("id"):
                report_observations[report["id"]] = EpicClient.observations_for_report(report)
        conditions = EpicClient.conditions_for_patient(patient_id)
        service_requests = EpicClient.service_requests_for_patient(patient_id)
        document_references = EpicClient.document_references_for_patient(patient_id)
        family_history = EpicClient.family_history_for_patient(patient_id)
    except Exception as e:
        error = str(e)
    return render_template(
        "pathology_patient.html", patient_id=patient_id, patient=patient,
        reports=reports, report_observations=report_observations, conditions=conditions,
        service_requests=service_requests, document_references=document_references,
        family_history=family_history, family_sections=family_history_sections(family_history),
        error=error,
    )


@app.route("/pathology/document/<document_id>")
def pathology_document(document_id):
    """Streams one attachment off an Epic DocumentReference — the
    Pathology Explorer's counterpart to /report/<report_id>/pdf.
    document_id is enough on its own (no patient_id needed) since
    EpicClient.get_document_reference() is a direct Read, not a
    Search."""
    index = int(request.args.get("index", 0))
    try:
        document = EpicClient.get_document_reference(document_id)
    except Exception as e:
        return f"Could not load document: {e}", 502

    contents = document.get("content") or []
    if index >= len(contents):
        abort(404, description="This document has no attachment at that index.")
    attachment = contents[index].get("attachment") or {}

    try:
        data, content_type = EpicClient.fetch_attachment_bytes(attachment)
    except Exception as e:
        return f"Could not fetch attachment: {e}", 502
    if data is None:
        abort(404, description="Attachment had neither inline data nor a URL.")

    filename = attachment.get("title") or f"{document_id}-{index}"
    return Response(
        data, mimetype=content_type or "application/octet-stream",
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
