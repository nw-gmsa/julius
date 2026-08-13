"""
Thin wrapper around a FHIR R4 server's REST API, conforming to the
NHS North West Genomics IG (https://nw-gmsa.github.io/en/), focused on
the resources you need for genomic test orders/reports:

  - Patient            (to search/identify a patient)
  - ServiceRequest      (genomic test ORDERS)
  - DiagnosticReport    (genomic test REPORTS)
  - Observation         (individual result values referenced by a report)

No FHIR client library is used on purpose — the R4 REST API is just
JSON over HTTP, and keeping this dependency-free makes it easy to see
exactly what's being requested and adapt it to your server's quirks.

Category codes below come straight from the IG's published profiles,
not guesses:
  - ServiceRequest.category:GenomicProcedure  -> SNOMED 116148004
    (https://nw-gmsa.github.io/en/StructureDefinition-ServiceRequest.html)
  - DiagnosticReport.category:Genetics        -> v2-0074 code "GE"
    (https://nw-gmsa.github.io/en/StructureDefinition-DiagnosticReport.html)
"""
import os
import csv
import io
import re
import time
import base64
import secrets
import uuid
import requests
from datetime import date, datetime, timezone
from urllib.parse import quote
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

SERVICE_REQUEST_CATEGORY = "http://snomed.info/sct|116148004"
DIAGNOSTIC_REPORT_CATEGORY = "http://terminology.hl7.org/CodeSystem/v2-0074|GE"

# _include params for the patient-scoped order/report searches, so a
# patient's requester/specimen/result resources come back in the same
# Bundle instead of one follow-up GET each. ":iterate" (R4 name for the
# older ":recurse" modifier) is needed to reach the Practitioner/
# Organization a PractitionerRole requester points to, one hop further.
SERVICE_REQUEST_INCLUDES = ["ServiceRequest:requester", "ServiceRequest:specimen"]
SERVICE_REQUEST_ITERATE_INCLUDES = ["PractitionerRole:practitioner", "PractitionerRole:organization"]
DIAGNOSTIC_REPORT_INCLUDES = ["DiagnosticReport:result", "DiagnosticReport:specimen"]

# ServiceRequest.status values (FHIR R4's request-status value set) other
# than "completed", comma-joined for FHIR search OR semantics — used by
# ctdna_orders() to fetch the "outstanding" bucket without pulling in every
# completed order too (that bucket is queried, and date-bound, separately).
NON_COMPLETED_STATUSES = "draft,active,on-hold,revoked,entered-in-error,unknown"


class FhirClient:
    def __init__(self, base_url=None, user=None, password=None, verify_ssl=None):
        self.base_url = (base_url or os.environ.get("FHIR_BASE_URL", "")).rstrip("/")
        if not self.base_url:
            raise ValueError(
                "FHIR_BASE_URL is not set (and no base_url was passed in) — "
                "requests would otherwise fail deep inside `requests` with a "
                "confusing MissingSchema error instead of a clear one. Set "
                "the FHIR_BASE_URL environment variable, e.g. "
                "https://192.168.1.62/healthconnect/cdr/fhir/r4"
            )
        # Basic auth, as configured for this server.
        self.user = user or os.environ.get("FHIR_USER", "sqluser")
        self.password = password or os.environ.get("FHIR_PASSWORD", "demo123")
        if verify_ssl is None:
            # Internal IP + likely self-signed cert -> default to NOT verifying.
            # Override with FHIR_VERIFY_SSL=true if your server has a real cert.
            verify_ssl = os.environ.get("FHIR_VERIFY_SSL", "false").lower() == "true"
        self.verify_ssl = verify_ssl
        self._ref_cache = {}  # reference string -> resolved resource (or None); process-lifetime only
        self._geocode_cache = {}  # normalized postcode -> (lat, lon) or None; process-lifetime only
        self._org_ics_cache = {}  # Organization.id -> ICB23NM name (or None); process-lifetime only

    def _auth(self):
        return HTTPBasicAuth(self.user, self.password) if self.user else None

    def is_production(self):
        """Best-effort guess at whether this client is pointed at a live
        production system, purely from FHIR_BASE_URL containing "prod"
        (case-insensitive) — there's no other environment signal
        available. Used to gate destructive per-patient actions (e.g. the
        patient clear-down screen) so they can't be fired against real
        patient records by mistake. Not a security boundary (anyone who
        can edit FHIR_BASE_URL or the URL bypasses it), just a guard rail
        against an accidental click."""
        return "prod" in self.base_url.lower()

    def verify_credentials(self):
        """Minimal authenticated request to confirm self.user/self.password
        are accepted by the FHIR server. Raises requests.HTTPError (e.g. a
        401/403) or requests.RequestException (connection/SSL failure) if
        not — callers (the login route) catch these to show an error rather
        than letting a bad login silently through.

        Uses `_summary=count` rather than a plain `_count=1` search: the
        latter still asks the server to find and build actual Patient
        resources before truncating to 1, and on a server with a large
        Patient table that's exactly the same failure mode that used to
        413 the ctDNA screen (see ctdna_orders() history) — an
        unauthenticated-feeling, unrelated-looking 413 on login that's
        really the server choking on result-set size, not anything about
        the credentials themselves. `_summary=count` asks the server to
        return just the total and skip materializing any resources at
        all, which is enough to prove the credentials were accepted."""
        self._get("Patient", params={"_summary": "count"})

    def _headers(self):
        return {"Accept": "application/fhir+json"}

    @staticmethod
    def _raise_for_status_with_detail(resp):
        """Like resp.raise_for_status(), but folds the response body's
        own detail into the exception message when there is one. A FHIR
        error response is normally an OperationOutcome with a
        human-readable issue[].diagnostics/.details.text explaining
        *why* — requests.HTTPError's default str() is just "<code>
        <reason> for url: <url>" (e.g. the unhelpful "413 Client Error:
        OK for url: ..." this app's error banners were showing, with no
        way to tell from the UI what the server actually objected to).
        Falls back to raw response text (truncated) if the body isn't a
        parseable OperationOutcome, and to plain raise_for_status()'s
        behaviour if there's no body at all."""
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            detail = None
            try:
                body = resp.json()
                if body.get("resourceType") == "OperationOutcome":
                    texts = []
                    for issue in body.get("issue", []):
                        text = issue.get("diagnostics") or (issue.get("details") or {}).get("text") or issue.get("code")
                        if text:
                            texts.append(text)
                    detail = "; ".join(texts) or None
            except ValueError:
                detail = (resp.text or "").strip()[:500] or None
            if detail:
                raise requests.HTTPError(f"{e} — {detail}", response=resp) from e
            raise

    def _get(self, path, params=None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = requests.get(
            url, headers=self._headers(), params=params or {},
            auth=self._auth(), verify=self.verify_ssl, timeout=15,
        )
        self._raise_for_status_with_detail(resp)
        return resp.json()

    def _delete(self, path):
        """DELETE a single resource by relative path (e.g.
        "ServiceRequest/123"). Returns True if the resource is gone
        afterwards (a successful delete, or a 404 — already gone counts as
        cleared), False on any other failure. Never raises: a clear-down
        should keep going and report what it could/couldn't delete rather
        than aborting at the first failure."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = requests.delete(
                url, headers=self._headers(),
                auth=self._auth(), verify=self.verify_ssl, timeout=15,
            )
            if resp.status_code == 404:
                return True
            resp.raise_for_status()
            return True
        except requests.RequestException:
            return False

    def _put(self, path, resource):
        """PUT a full resource body to a relative path (e.g.
        "Organization/123") — used for corrections that update an existing
        resource in place (see update_organization_name()). Raises
        requests.HTTPError on failure, unlike _delete()'s swallow-and-report
        style: a write that fails here should stop the caller rather than
        being silently counted as done. Some servers respond to a
        successful PUT with an empty body (200 with nothing, or 204 No
        Content) rather than the updated resource — real behaviour seen
        against this app's own configured server — so a 2xx with no
        parseable JSON body is still treated as success and returns None
        rather than raising on the body parse."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = requests.put(
            url, json=resource,
            headers={**self._headers(), "Content-Type": "application/fhir+json"},
            auth=self._auth(), verify=self.verify_ssl, timeout=15,
        )
        self._raise_for_status_with_detail(resp)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _post(self, path, resource):
        """POST a new resource to a relative collection path (e.g.
        "Practitioner") — used for creating a resource that doesn't exist
        yet (see import_econcur() below), the create-side counterpart to
        _put()'s update-in-place. Raises requests.HTTPError on failure,
        same as _put(). Returns the created resource (with its
        server-assigned `id`) when the server's response body has one;
        some servers reply 201 with an empty body and only a `Location`
        header pointing at the new resource instead (real behaviour seen
        against this app's own configured server for _put(), so handled
        the same way here) — in that case the id is parsed back out of the
        Location header instead of being left unset, since every caller
        needs it to link a just-created resource from later rows/writes."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = requests.post(
            url, json=resource,
            headers={**self._headers(), "Content-Type": "application/fhir+json"},
            auth=self._auth(), verify=self.verify_ssl, timeout=15,
        )
        self._raise_for_status_with_detail(resp)
        if resp.content:
            try:
                body = resp.json()
                if body.get("id"):
                    return body
            except ValueError:
                pass
        location = resp.headers.get("Location") or resp.headers.get("Content-Location") or ""
        match = re.search(rf"{resource['resourceType']}/([^/]+)", location)
        if match:
            return {**resource, "id": match.group(1)}
        return None

    @staticmethod
    def _entries(bundle):
        """Pull resources out of a FHIR Bundle, ignoring missing/empty ones."""
        return [e["resource"] for e in bundle.get("entry", []) if "resource" in e]

    @staticmethod
    def _split_bundle(bundle):
        """
        Split a Bundle's entries into (matches, included) using
        Bundle.entry.search.mode: "match" is a primary search result,
        "include" is a resource pulled in via _include/_include:iterate.
        Entries missing search.mode entirely are treated as matches (that's
        the default meaning when the element is absent).
        """
        matches, included = [], []
        for entry in bundle.get("entry", []):
            resource = entry.get("resource")
            if not resource:
                continue
            mode = (entry.get("search") or {}).get("mode", "match")
            (included if mode == "include" else matches).append(resource)
        return matches, included

    def _cache_included(self, included):
        """Pre-populate the reference cache from a Bundle's _include'd
        resources, so resolve_reference() serves them without a follow-up
        GET for the rest of this process's lifetime."""
        for resource in included:
            rtype, rid = resource.get("resourceType"), resource.get("id")
            if rtype and rid:
                self._ref_cache[f"{rtype}/{rid}"] = resource

    #: Floor for _get_with_count_backoff()'s halving retry below — if even
    #: this small a page still 413s, something other than result-set size
    #: is wrong, so it's left to raise rather than retrying forever.
    MIN_SEARCH_COUNT = 5

    #: This FHIR server (InterSystems IRIS) rejects a search whose only
    #: parameter is `_count` — a genuinely unfiltered "give me every X
    #: system-wide" query 413s regardless of `_count`'s value, which is
    #: why _get_with_count_backoff()'s halving retry alone doesn't fix
    #: this specific failure (confirmed: shrinking `_count` made no
    #: difference — the request needs an actual search parameter, not a
    #: smaller page). `_search_all_split()` below adds this trivially-
    #: true `_lastUpdated` filter automatically whenever `params` would
    #: otherwise be `_count`-only, rather than every "fetch every X
    #: system-wide" caller needing to remember to add a throwaway filter
    #: itself. `gt1900-01-01` matches every real resource (a valid
    #: `meta.lastUpdated` can't predate the FHIR server's own existence),
    #: so this doesn't drop any data — it's a filter in form only.
    UNFILTERED_SEARCH_FALLBACK_PARAMS = {"_lastUpdated": "gt1900-01-01"}

    def _get_with_count_backoff(self, resource_type, params):
        """GET `resource_type` with `params`, halving `_count` and
        retrying on a 413 rather than failing outright — seen on a real
        server for both Practitioner (import_econcur()'s preloads) and
        Patient (patients_in_nhs_number_ranges()) searches, even at a
        fairly modest _count=100. Same "server chokes on materializing a
        result set, not on anything about the query itself" failure mode
        verify_credentials()'s _summary=count workaround exists for (see
        its docstring) — that workaround only proves credentials are
        accepted without materializing any resources, so it doesn't help
        here, where the caller actually needs the resources back. Only
        retries when `params` has a `_count` to shrink, and only on 413;
        re-raises immediately once `_count` is down to MIN_SEARCH_COUNT,
        or for any other status."""
        request_params = dict(params)
        while True:
            try:
                return self._get(resource_type, request_params)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                count = request_params.get("_count")
                if status != 413 or not count or count <= self.MIN_SEARCH_COUNT:
                    raise
                request_params["_count"] = max(count // 2, self.MIN_SEARCH_COUNT)

    def _search_all_split(self, resource_type, params, max_pages=10):
        """Like _search_all(), but keeps _include/_revinclude'd resources
        separate from primary matches (see _split_bundle), across however
        many pages are followed. Used for system-wide queries that aren't
        scoped to one patient and can span many results.

        If `params` has no real filter beyond `_count` (a genuine
        "fetch every X system-wide" query), UNFILTERED_SEARCH_FALLBACK_PARAMS
        is added first — this server rejects a `_count`-only search
        outright (see that constant's docstring). The first page is then
        fetched via _get_with_count_backoff() (see its docstring) so a
        413 from an oversized page *also* shrinks `_count` and retries,
        independently of the fallback-filter fix above — subsequent
        pages are followed via Bundle.link[rel=next] URLs, which already
        bake in whatever params the first page's request succeeded with.
        """
        request_params = dict(params)
        if set(request_params) <= {"_count"}:
            request_params.update(self.UNFILTERED_SEARCH_FALLBACK_PARAMS)
        all_matches, all_included = [], []
        bundle = self._get_with_count_backoff(resource_type, request_params)
        matches, included = self._split_bundle(bundle)
        all_matches.extend(matches)
        all_included.extend(included)
        pages = 1
        while pages < max_pages:
            next_url = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
            if not next_url:
                break
            resp = requests.get(next_url, headers=self._headers(), auth=self._auth(),
                                 verify=self.verify_ssl, timeout=15)
            self._raise_for_status_with_detail(resp)
            bundle = resp.json()
            matches, included = self._split_bundle(bundle)
            all_matches.extend(matches)
            all_included.extend(included)
            pages += 1
        return all_matches, all_included

    def _search_all(self, resource_type, params, max_pages=10):
        """Search + follow Bundle.link[rel=next] pages, up to max_pages,
        returning every resource in the response regardless of
        match/include (see _search_all_split for a version that keeps them
        apart)."""
        matches, _included = self._search_all_split(resource_type, params, max_pages)
        return matches

    # ---- Patient -----------------------------------------------------

    def search_patients(self, name=None, patient_id=None, nhs_number=None):
        if patient_id:
            try:
                return [self._get(f"Patient/{patient_id}")]
            except requests.HTTPError:
                return []
        if nhs_number:
            # Patients in this IG carry an NHSIdentifier; search by identifier
            # rather than by name for a reliable, unambiguous match.
            bundle = self._get("Patient", {"identifier": nhs_number, "_count": 20})
            return self._entries(bundle)
        bundle = self._get("Patient", {"name": name, "_count": 20})
        return self._entries(bundle)

    def find_orders_by_identifier(self, value):
        """
        ServiceRequest resources whose `identifier` matches `value` — used
        by the search screen's order/test-number lookup. FHIR's
        `identifier` search param matches by value alone when no
        `system|value` pipe is given (same convention search_patients()
        already uses for nhs_number), so this catches a placer number, a
        filler number, or any other identifier on the order without the
        caller needing to know which kind it is.
        """
        bundle = self._get("ServiceRequest", {"identifier": value, "_count": 20})
        return self._entries(bundle)

    def find_reports_by_identifier(self, value):
        """DiagnosticReport resources whose `identifier` matches `value` —
        the report-side counterpart to find_orders_by_identifier(), e.g.
        for an iGene report identifier."""
        bundle = self._get("DiagnosticReport", {"identifier": value, "_count": 20})
        return self._entries(bundle)

    def get_patient(self, patient_id):
        """Fetch a single Patient resource by ID (used by the patient page's
        demographics section, which needs the resource itself rather than
        just the ID string it's routed on)."""
        return self._get(f"Patient/{patient_id}")

    #: NHS number identifier system (standard, not IG-specific).
    NHS_NUMBER_SYSTEM = "https://fhir.nhs.uk/Id/nhs-number"

    #: iGene patient identifier system (NW Genomics IG-specific).
    IGENE_PATIENT_IDENTIFIER_SYSTEM = "https://fhir.nwgenomics.nhs.uk/Identifier/IGENE-PatientIdentifier"

    #: HL7 v2-0203 identifier-type codes used on Patient.identifier: "MR"
    #: (medical record number, one per assigning organisation) and "PI"
    #: (patient internal identifier — used here for the iGene patient ID,
    #: which also carries its own IG-specific system, matched alongside the
    #: type since "PI" alone is a generic HL7 type shared by other systems).
    MEDICAL_RECORD_NUMBER_TYPE = "MR"
    PATIENT_IDENTIFIER_TYPE = "PI"

    @classmethod
    def nhs_number(cls, patient):
        """The patient's NHS number (see NHS_NUMBER_SYSTEM)."""
        return cls._identifier_value(patient, cls.NHS_NUMBER_SYSTEM)

    #: CHI (Community Health Index) number identifier system — Scotland's
    #: equivalent of an NHS number. Not IG-specific; a patient registered
    #: in Scotland but seen by an NW England genomics service could carry
    #: this instead of (or as well as) an NHS number. System URI not
    #: confirmed against a real server.
    CHI_NUMBER_SYSTEM = "https://fhir.hl7.org.uk/Id/chi-number"

    @classmethod
    def chi_number(cls, patient):
        """The patient's CHI number (see CHI_NUMBER_SYSTEM)."""
        return cls._identifier_value(patient, cls.CHI_NUMBER_SYSTEM)

    @classmethod
    def nhs_or_chi_number(cls, patient):
        """(label, value) for whichever of NHS number / CHI number is
        present on `patient` — NHS number preferred, CHI number as a
        fallback for patients who don't carry one (e.g. Scottish
        patients). Returns ("NHS number", None) if neither is present, so
        callers always get a consistent label to show even with no value."""
        nhs = cls.nhs_number(patient)
        if nhs:
            return "NHS number", nhs
        chi = cls.chi_number(patient)
        if chi:
            return "CHI number", chi
        return "NHS number", None

    #: UK Core extension on the NHS number identifier itself, carrying its
    #: trace/verification status. "01" ("Number present and verified") is
    #: the only fully-trusted value in the NHS Data Dictionary's NHS
    #: NUMBER STATUS INDICATOR CODE value set — other codes (e.g. "02"
    #: "Number present but not traced", "03" "Trace required") mean the
    #: number hasn't been confirmed against PDS. Unconfirmed whether this
    #: server populates this extension at all — see nhs_number_verification_status().
    NHS_NUMBER_VERIFICATION_STATUS_EXTENSION = "https://fhir.hl7.org.uk/StructureDefinition/Extension-UKCore-NHSNumberVerificationStatus"

    @classmethod
    def nhs_number_verification_status(cls, patient):
        """The NHS number verification status code (see
        NHS_NUMBER_VERIFICATION_STATUS_EXTENSION), read off the NHS
        number identifier's own `.extension` array — not the identifier
        list generally, since this extension is specifically scoped to
        that one identifier entry per the UK Core profile. Returns None
        if there's no NHS number identifier, it has no verification
        status extension at all, or the extension has no coded value —
        callers should treat None the same as "not verified" (patient.html
        warns whenever this isn't exactly "01")."""
        if not patient:
            return None
        for ident in patient.get("identifier", []):
            if ident.get("system") != cls.NHS_NUMBER_SYSTEM:
                continue
            for ext in ident.get("extension", []):
                if ext.get("url") == cls.NHS_NUMBER_VERIFICATION_STATUS_EXTENSION:
                    coding = (ext.get("valueCodeableConcept") or {}).get("coding", [])
                    return coding[0].get("code") if coding else None
        return None

    @classmethod
    def igene_patient_identifier(cls, patient):
        """The iGene patient identifier — type PI, matched by its IG-specific
        system (IGENE_PATIENT_IDENTIFIER_SYSTEM) so a differently-sourced PI
        identifier (if any) isn't picked up by mistake."""
        if not patient:
            return None
        for ident in patient.get("identifier", []):
            if ident.get("system") != cls.IGENE_PATIENT_IDENTIFIER_SYSTEM:
                continue
            codings = (ident.get("type") or {}).get("coding", [])
            if any(c.get("code") == cls.PATIENT_IDENTIFIER_TYPE for c in codings):
                return ident.get("value")
        return None

    def medical_record_numbers(self, patient):
        """
        Every medical record number (HL7 v2-0203 type "MR") on a Patient, as
        [{"value", "assigner_name", "assigner_ods", "assigner_display"}, ...]
        — a patient can carry more than one MRN, one per assigning
        organisation (e.g. a separate MRN at each trust that's treated
        them), so this returns a list rather than a single value like
        nhs_number()/igene_patient_identifier() above. `assigner_display`
        is `assigner_name`/`assigner_ods` combined via _name_with_ods() —
        order_view.html's single "Hospital number" cell uses that; the two
        separate fields exist for patient.html's dedicated columns.

        `identifier.assigner` is a Reference, which per FHIR can point at an
        Organization either by literal `.reference` (fetched and passed to
        organisation_ods_code() for its ODS code) or by an inline
        `.identifier` — a logical reference with no resource to fetch, where
        this server puts the ODS code directly as `assigner.identifier.value`.
        resolve_organisation_ods() checks both, since either shape is valid
        and this server appears to use the latter.
        """
        results = []
        if not patient:
            return results
        for ident in patient.get("identifier", []):
            codings = (ident.get("type") or {}).get("coding", [])
            if not any(c.get("code") == self.MEDICAL_RECORD_NUMBER_TYPE for c in codings):
                continue
            assigner_ref = ident.get("assigner") or {}
            assigner = self.resolve_reference(assigner_ref) if assigner_ref else None
            assigner_name = (assigner.get("name") if assigner else None) or assigner_ref.get("display")
            assigner_ods = self.resolve_organisation_ods(assigner_ref)
            results.append({
                "value": ident.get("value"),
                "assigner_name": assigner_name,
                "assigner_ods": assigner_ods,
                "assigner_display": self._name_with_ods(assigner_name, assigner_ods),
            })
        return results

    # ---- Genomic test orders (ServiceRequest) -------------------------

    def lab_orders_for_patient(self, patient_id):
        base_params = {
            "patient": patient_id,
            "_sort": "-authored",
            "_count": 50,
            "_include": SERVICE_REQUEST_INCLUDES,
            "_include:iterate": SERVICE_REQUEST_ITERATE_INCLUDES,
        }
        try:
            bundle = self._get("ServiceRequest", {**base_params, "category": SERVICE_REQUEST_CATEGORY})
            matches, included = self._split_bundle(bundle)
            if matches:
                self._cache_included(included)
                return matches
        except requests.HTTPError:
            pass
        # Fallback without the category filter, in case this server instance
        # doesn't populate category or slices it differently.
        bundle = self._get("ServiceRequest", base_params)
        matches, included = self._split_bundle(bundle)
        self._cache_included(included)
        return matches

    @staticmethod
    def build_order_chains(orders):
        """
        Arrange a flat list of ServiceRequest orders into parent/child chains
        via `basedOn` (the IG's link from a reanalysis/cascade-testing
        request back to its originating order). Returns a list of root nodes
        (orders with no resolvable parent within this list), each shaped
        {"order": order, "children": [...]} recursively.

        A parent is only resolved if the referenced ServiceRequest is itself
        present in `orders` — if it's missing (e.g. paginated away), the
        order becomes a root rather than being dropped. Would-be cycles
        (shouldn't happen per spec, but `basedOn` is server-supplied data)
        are also treated as roots instead of linked, so one bad reference
        can't hang rendering.
        """
        by_id = {o["id"]: o for o in orders if o.get("id")}
        nodes = {oid: {"order": o, "children": []} for oid, o in by_id.items()}

        def parent_id(order):
            for ref in order.get("basedOn", []):
                ref_str = ref.get("reference", "")
                if ref_str.startswith("ServiceRequest/"):
                    candidate = ref_str.split("/", 1)[1]
                    if candidate in by_id and candidate != order.get("id"):
                        return candidate
            return None

        def creates_cycle(child_id, candidate_parent_id):
            current, steps = candidate_parent_id, 0
            while current is not None and steps <= len(by_id):
                if current == child_id:
                    return True
                current = parent_id(by_id[current])
                steps += 1
            return False

        roots = []
        for oid, order in by_id.items():
            pid = parent_id(order)
            if pid and not creates_cycle(oid, pid):
                nodes[pid]["children"].append(nodes[oid])
            else:
                roots.append(nodes[oid])
        return roots

    def _active_orders_with_intent(self, intent, start, end):
        """
        Shared implementation behind active_filler_orders()/
        active_placer_orders(): active genomic test orders (ServiceRequest)
        whose `intent` matches, system-wide, for the work orders/test
        orders screens. `intent` is a single value, or several comma-joined
        into one string for FHIR search's OR semantics — a repeated
        `intent=` *parameter name* means AND instead (as used elsewhere in
        this file for date ranges), which no single resource's one `intent`
        value could ever satisfy, so multiple intents must go in one
        comma-joined param, not a list.

        `start`/`end` (inclusive ISO dates) are required, not optional —
        unlike ctdna_orders()'s start/end, which can be left unbounded.
        This query used to have no date bound at all (only `status=active`
        + `intent`), which is exactly the same unbounded-result-set 413
        that ctdna_orders() was rewritten to avoid — a server with a large
        active-order backlog could 413 here just as easily. Bound by
        `authored`, same convention as orders_in_range()/ctdna_orders().

        Not scoped to one patient, so each order's specimen/patient/
        requester come back in the same query via `_include` (same shape
        as ctdna_orders()), and results paginate up to
        `_search_all_split`'s default cap (1,000 records) — see README for
        what to do if that's ever hit.

        Like ctdna_orders(), orders are identified by `resourceType` across
        `matches + included` combined rather than by trusting
        `Bundle.entry.search.mode` (see that method's docstring for the
        real bug this pattern fixes on servers that don't reliably tag it).
        """
        base_params = {
            "intent": intent,
            "status": "active",
            "authored": [f"ge{start}", f"le{end}"],
            "_count": 100,
            "_include": ["ServiceRequest:specimen", "ServiceRequest:patient", "ServiceRequest:requester"],
            "_include:iterate": SERVICE_REQUEST_ITERATE_INCLUDES,
        }
        try:
            matches, included = self._search_all_split(
                "ServiceRequest", {**base_params, "category": SERVICE_REQUEST_CATEGORY})
        except requests.HTTPError:
            matches, included = [], []
        if not matches:
            matches, included = self._search_all_split("ServiceRequest", base_params)
        self._cache_included(matches + included)

        orders_by_id = {
            o["id"]: o for o in (matches + included)
            if o.get("resourceType") == "ServiceRequest" and o.get("id")
        }
        return list(orders_by_id.values())

    def active_filler_orders(self, start, end):
        """All active genomic test orders with `intent=filler-order` — i.e.
        orders as seen from the filler/lab system's side. Used by the work
        orders screen. See _active_orders_with_intent()."""
        return self._active_orders_with_intent("filler-order", start, end)

    def active_placer_orders(self, start, end):
        """All active genomic test orders with `intent` of "order" or
        "original-order" — i.e. orders as seen from the placer/requesting
        system's side, as opposed to active_filler_orders()'s filler-order
        orders. Used by the test orders screen. See
        _active_orders_with_intent()."""
        return self._active_orders_with_intent("order,original-order", start, end)

    # ---- Genomic test reports (DiagnosticReport + Observation) --------

    def lab_reports_for_patient(self, patient_id):
        base_params = {
            "patient": patient_id,
            "_sort": "-date",
            "_count": 50,
            "_include": DIAGNOSTIC_REPORT_INCLUDES,
        }
        try:
            bundle = self._get("DiagnosticReport", {**base_params, "category": DIAGNOSTIC_REPORT_CATEGORY})
            matches, included = self._split_bundle(bundle)
            if matches:
                self._cache_included(included)
                return matches
        except requests.HTTPError:
            pass
        bundle = self._get("DiagnosticReport", base_params)
        matches, included = self._split_bundle(bundle)
        self._cache_included(included)
        return matches

    def observations_for_report(self, report):
        """Resolve the Observation resources a DiagnosticReport points to.
        Goes through resolve_reference() (the process-wide reference cache),
        so Observations already pulled in by lab_reports_for_patient's
        `_include=DiagnosticReport:result` are served from cache instead of
        being re-fetched one at a time."""
        obs = []
        for ref in report.get("result", []):
            resource = self.resolve_reference(ref)
            if resource:
                obs.append(resource)
        return obs

    def get_report(self, report_id):
        """Fetch a single DiagnosticReport by ID (used to re-fetch presentedForm
        when serving a PDF, since we don't keep search results in server state)."""
        return self._get(f"DiagnosticReport/{report_id}")

    def get_order(self, order_id):
        """Fetch a single ServiceRequest by ID — used by the "view order
        form" screen (/order/<id>), reached from a patient's order table
        rather than kept in server state between requests."""
        return self._get(f"ServiceRequest/{order_id}")

    # ---- AuditEvent (audit trail) -------------------------------------

    #: FHIR R4's own "audit-event-action" ValueSet — not deployment-
    #: specific, so hardcoded rather than fetched (same reasoning as
    #: ASK_AT_ORDER_ENTRY_QUESTIONS being a small, stable, spec-defined
    #: set not worth a live lookup for).
    #: https://hl7.org/fhir/R4/valueset-audit-event-action.html
    AUDIT_EVENT_ACTIONS = {
        "C": "Create", "R": "Read/View/Print", "U": "Update",
        "D": "Delete", "E": "Execute",
    }

    #: FHIR R4's "audit-event-outcome" ValueSet, same reasoning as above.
    #: https://hl7.org/fhir/R4/valueset-audit-event-outcome.html
    AUDIT_EVENT_OUTCOMES = {
        "0": "Success", "4": "Minor failure",
        "8": "Serious failure", "12": "Major failure",
    }

    @classmethod
    def audit_action_label(cls, code):
        return cls.AUDIT_EVENT_ACTIONS.get(code, code or "—")

    @classmethod
    def audit_outcome_label(cls, code):
        return cls.AUDIT_EVENT_OUTCOMES.get(code, code or "—")

    def audit_events_for_patient(self, patient_id, start=None, end=None, max_pages=10):
        """
        AuditEvent history for one patient — who accessed/changed their
        record, when, and how, per this FHIR server's own audit log.

        AuditEvent has two standard R4 search params that can scope by
        patient: the composite `patient` param (defined to resolve either
        `agent.who` or `entity.what` to a Patient — the one actually meant
        for "everything about this patient's audit trail") and the
        narrower `entity` param (matches `entity.what` directly). `patient`
        is tried first, falling back to `entity=Patient/<id>` for a server
        that indexes AuditEvent.entity but not the composite `patient`
        param — same try-then-fall-back stance the category-code searches
        (lab_orders_for_patient() etc.) use elsewhere in this file; neither
        has been confirmed against a real server yet.

        Bounded by `date` (AuditEvent.recorded) when start/end are given,
        same [ge, le] repeated-param convention orders_in_range()/
        reports_in_range() use — unlike those, both are optional here
        since this is already patient-scoped, not a system-wide query, so
        an unbounded call isn't the 413 risk those were rewritten to avoid.
        """
        params = {"_sort": "-date", "_count": 50}
        if start and end:
            params["date"] = [f"ge{start}", f"le{end}"]
        try:
            matches = self._search_all("AuditEvent", {**params, "patient": patient_id}, max_pages=max_pages)
            if matches:
                return matches
        except requests.HTTPError:
            pass
        return self._search_all("AuditEvent", {**params, "entity": f"Patient/{patient_id}"}, max_pages=max_pages)

    def audit_event_agent_display(self, agent):
        """Best-effort display name for one AuditEvent.agent entry —
        prefers the entry's own `.name` (the field the AuditEvent spec
        specifically defines for this: "Human-meaningful name for the
        agent"), then a resolved `.who` reference's Patient/Practitioner-
        style `.name[]`, then that reference's own inline `.display`/
        `.identifier`, same resolve-then-fall-back-to-logical-reference
        stance `_resolve_practitioner_display()` uses for a requester's
        practitioner reference."""
        if agent.get("name"):
            return agent["name"]
        who = agent.get("who") or {}
        resource = self.resolve_reference(who)
        if resource:
            names = resource.get("name", [])
            if names:
                n = names[0]
                given = " ".join(n.get("given", []))
                family = n.get("family", "")
                full = f"{given} {family}".strip()
                if full:
                    return full
        if who.get("display"):
            return who["display"]
        ident_value = (who.get("identifier") or {}).get("value")
        if ident_value:
            return ident_value
        return "Unknown"

    @staticmethod
    def audit_event_entity_display(entity):
        """Best-effort label for one AuditEvent.entity entry — `.name`,
        then `.description`, then whatever `.what` carries inline
        (`.identifier.value`, then `.display`/`.reference`); entity.what
        is deliberately not resolved via resolve_reference() the way
        agent.who is above, since an entity is very often the patient
        themself or a non-Patient/Practitioner resource (a
        ServiceRequest, a DiagnosticReport, ...) with no single shared
        "name" shape to extract. The `.identifier.value` fallback matters
        for id-carrying entities specifically (audit_event_message_id()/
        audit_event_correlation_id() above) — a request/correlation ID is
        plausibly modelled as a logical identifier rather than free text,
        so it's checked before falling through to `.display`/`.reference`."""
        if entity.get("name"):
            return entity["name"]
        if entity.get("description"):
            return entity["description"]
        what = entity.get("what") or {}
        ident_value = (what.get("identifier") or {}).get("value")
        return ident_value or what.get("display") or what.get("reference") or "—"

    @staticmethod
    def audit_event_source_display(event):
        """The system/process that recorded the event. Prefers
        AuditEvent.source.observer's inline display (source.observer is
        typically a Device with no more useful a name than its own
        .display already carries; not resolved via resolve_reference()),
        falling back to AuditEvent.source.site — a plain string field
        confirmed populated on this deployment even when
        source.observer.display isn't (seen on patient 24786's events).

        The raw value is shaped like
        "CDR.Production.CDRDevelopment:Operation.GenomicDataRepository" —
        abbreviated to "<system> <process>": the part before the first
        "." (the system) and the part after the last "." (the process),
        e.g. "CDR GenomicDataRepository". Falls back to the value
        unchanged if it doesn't contain a "." to split on."""
        source = event.get("source") or {}
        observer = source.get("observer") or {}
        display = observer.get("display") or source.get("site")
        if not display:
            return "—"
        if "." not in display:
            return display
        system = display.split(".", 1)[0]
        process = display.rsplit(".", 1)[-1]
        return f"{system} {process}"

    #: DICOM audit source role code "Destination" (CID 402) — the
    #: AuditEvent.agent entry representing where the recorded action's
    #: data went. System unconfirmed against this deployment (not one of
    #: the terminology.hl7.org-hosted systems this app otherwise pins),
    #: so matched by code alone, same "code confirmed, system not" stance
    #: as AUDIT_ENTITY_MESSAGE_ID_CODE/AUDIT_ENTITY_CORRELATION_ID_CODE.
    AUDIT_AGENT_DESTINATION_TYPE_CODE = "110152"

    @classmethod
    def audit_event_destination_display(cls, event):
        """`.name` of the AuditEvent.agent entry acting as the message
        destination — identified by agent.type.coding[].code ==
        AUDIT_AGENT_DESTINATION_TYPE_CODE. Deliberately shows only
        agent.name, with no fallback to `.who`/`.identifier` the way
        audit_event_agent_display() has, since agent.name is specifically
        the field asked for here."""
        for agent in event.get("agent", []):
            atype = agent.get("type") or {}
            codes = [coding.get("code") for coding in atype.get("coding", [])]
            if cls.AUDIT_AGENT_DESTINATION_TYPE_CODE in codes:
                return agent.get("name") or "—"
        return "—"

    #: This deployment appears to carry the originating request's message
    #: ID / correlation ID as their own AuditEvent.entity entries, each
    #: identified by entity.type.code — local codes, system unconfirmed
    #: (same "code confirmed, system not" situation as BCRABL_CODE), so
    #: matched by code alone regardless of system, same as
    #: _is_bcrabl_report(). Not confirmed which entity field (`.name`,
    #: `.description`, `.what.identifier`, ...) actually carries the ID
    #: value itself either — audit_event_message_id()/
    #: audit_event_correlation_id() below check all of them via
    #: audit_event_entity_display() plus an identifier fallback.
    AUDIT_ENTITY_MESSAGE_ID_CODE = "XrequestId"
    AUDIT_ENTITY_CORRELATION_ID_CODE = "XcorrelationId"

    #: FHIR R4's own "audit-entity-type" CodeSystem
    #: (https://hl7.org/fhir/R4/v3/AuditEntityType/cs.html) — code "2" is
    #: "System Object", the code this deployment's query-carrying entity
    #: uses. Unlike the two local codes above, both the system and the
    #: code here are spec-fixed, so matched on both, not code alone.
    AUDIT_ENTITY_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/audit-entity-type"
    AUDIT_ENTITY_QUERY_TYPE_CODE = "2"

    @staticmethod
    def _audit_entity_by_type_code(event, code, system=None):
        """First AuditEvent.entity whose `.type.code` matches `code` —
        and, when `system` is given, whose `.type.system` matches too.
        entity.type is a bare Coding (not a CodeableConcept), so this
        checks it directly rather than going through a `.coding[]` list."""
        for entity in event.get("entity", []):
            etype = entity.get("type") or {}
            if etype.get("code") != code:
                continue
            if system is not None and etype.get("system") != system:
                continue
            return entity
        return None

    @classmethod
    def audit_event_message_id(cls, event):
        entity = cls._audit_entity_by_type_code(event, cls.AUDIT_ENTITY_MESSAGE_ID_CODE)
        return cls.audit_event_entity_display(entity) if entity else "—"

    @classmethod
    def audit_event_correlation_id(cls, event):
        entity = cls._audit_entity_by_type_code(event, cls.AUDIT_ENTITY_CORRELATION_ID_CODE)
        return cls.audit_event_entity_display(entity) if entity else "—"

    @classmethod
    def audit_event_query_text(cls, event):
        """The query-type entity's `.query` (base64Binary per spec),
        base64-decoded to plain text — the entity is found via the
        FHIR-standard audit-entity-type code "2" ("System Object"), see
        AUDIT_ENTITY_TYPE_SYSTEM/AUDIT_ENTITY_QUERY_TYPE_CODE above.
        Returns None (not "—") when there's no such entity or it carries
        no query, so the template can tell "no query entity" apart from
        "query entity present but undecodable"; falls back to the raw
        (undecoded) value if it isn't valid base64, rather than hiding a
        malformed-but-present value entirely."""
        entity = cls._audit_entity_by_type_code(
            event, cls.AUDIT_ENTITY_QUERY_TYPE_CODE, cls.AUDIT_ENTITY_TYPE_SYSTEM)
        query = entity.get("query") if entity else None
        if not query:
            return None
        try:
            return base64.b64decode(query).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return query

    def audit_events_in_range(self, start, end, max_pages=10):
        """
        Every AuditEvent system-wide, bounded by `date` (AuditEvent.recorded)
        within [start, end] — used by the admin screen's "delete all
        AuditEvents" action. Unlike audit_events_for_patient() above,
        start/end are **required**, not optional, for the same reason
        _active_orders_with_intent()'s are: an unbounded system-wide
        AuditEvent query is exactly the shape that's 413'd on this server
        before (see "413s on unfiltered system-wide searches") — an audit
        log is plausibly the largest table on the whole server, so this is
        the last place to risk an unbounded fetch.
        """
        params = {"_sort": "-date", "_count": 100, "date": [f"ge{start}", f"le{end}"]}
        return self._search_all("AuditEvent", params, max_pages=max_pages)

    def clear_down_audit_events_for_patient(self, patient_id):
        """DELETE every AuditEvent for one patient (audit_events_for_patient()
        with no date bound — safe unbounded here since it's patient-scoped,
        not system-wide, same reasoning as that method's own docstring).
        Returns {"deleted": [...], "failed": [...]} like clear_down_patient()."""
        return self._delete_resources("AuditEvent", self.audit_events_for_patient(patient_id))

    def clear_down_audit_events_in_range(self, start, end):
        """DELETE every AuditEvent system-wide within [start, end] (see
        audit_events_in_range()). Returns {"deleted": [...], "failed":
        [...]} like clear_down_patient()."""
        return self._delete_resources("AuditEvent", self.audit_events_in_range(start, end))

    #: Cepheid GeneXpert BCR-ABL1 quantitative monitoring test code. Unlike
    #: ctDNA (no confirmed code at all, so text-matched) or the Genomic Test
    #: Directory code (a confirmed system, so system-matched), this is a
    #: known exact code value but not a confirmed system, so
    #: _is_bcrabl_report() matches by `coding[].code` regardless of system.
    BCRABL_CODE = "BCRABL"

    #: LOINC code for BCR-ABL1 fusion transcript quantitation — an
    #: alternative to BCRABL_CODE some servers may use instead (or as well
    #: as) the local/Cepheid code above. Checked in addition to it, not
    #: instead of it, same reasoning as CTDNA_TEST_DIRECTORY_CODES growing
    #: over time rather than being a single fixed value.
    BCRABL_LOINC_CODE = "69380-4"

    #: Both known BCR-ABL1 codes, for _is_bcrabl_report() and the `code=`
    #: FHIR search parameter in bcrabl_reports() below.
    BCRABL_CODES = (BCRABL_CODE, BCRABL_LOINC_CODE)

    @classmethod
    def _is_bcrabl_report(cls, report):
        codings = (report.get("code") or {}).get("coding", [])
        return any(c.get("code") in cls.BCRABL_CODES for c in codings)

    @classmethod
    def _bcrabl_code_search_value(cls):
        """BCRABL_CODES as a FHIR token-search value — bare codes (no
        `system|` prefix), comma-joined for OR, since the coding system is
        unconfirmed and _is_bcrabl_report() itself matches on `code`
        regardless of system (see BCRABL_CODE)."""
        return ",".join(cls.BCRABL_CODES)

    def bcrabl_reports(self, start_date=None, end_date=None):
        """
        All DiagnosticReport resources system-wide with a BCRABL code (see
        _is_bcrabl_report) — the Cepheid Test Results screen. Paginated
        like other system-wide queries in this file (see README for the
        pagination cap).

        Optionally bounded to DiagnosticReport.date within
        [start_date, end_date] (ISO dates, same convention as
        orders_in_range()/reports_in_range()) — pass neither to fall back
        to the old unbounded-by-date query.

        `code` (BCRABL_CODES, via _bcrabl_code_search_value()) is a
        required part of this query, not a client-side-only filter — this
        used to be a `category=Genetics` search with no code restriction
        at all, filtered down to BCRABL reports only after fetching
        everything, which is exactly what 413'd on a live server (same
        class of bug ctdna_orders()'s DiagnosticReport-side query hit).
        `_is_bcrabl_report()` is still applied after fetching (rather than
        trusting the search to be exact) since a bare-code token search
        can't distinguish "this coding's code is BCRABL" from a
        coincidental match elsewhere on the resource, but the FHIR query
        itself is now what keeps the result set small, not just the
        client-side filter.

        Each report's specimen/patient/result(Observation) come back in
        the same query via `_include`, along with the originating
        ServiceRequest via `_include=DiagnosticReport:based-on` (forward
        include — the report references the order, so no revinclude is
        needed here, unlike ctdna_orders() which searches from the
        ServiceRequest side) and that order's own specimen via
        `_include:iterate`, in case a server attaches specimen there
        instead of on the report.

        Like ctdna_orders()/active_filler_orders(), reports are identified
        by `resourceType` across `matches + included` combined rather than
        by trusting `Bundle.entry.search.mode`.
        """
        base_params = {
            "_count": 100,
            "code": self._bcrabl_code_search_value(),
            "_include": [
                "DiagnosticReport:specimen", "DiagnosticReport:patient",
                "DiagnosticReport:result", "DiagnosticReport:based-on",
            ],
            "_include:iterate": ["ServiceRequest:specimen"],
        }
        if start_date and end_date:
            base_params["date"] = [f"ge{start_date}", f"le{end_date}"]
        try:
            matches, included = self._search_all_split(
                "DiagnosticReport", {**base_params, "category": DIAGNOSTIC_REPORT_CATEGORY})
        except requests.HTTPError:
            matches, included = [], []
        if not matches:
            matches, included = self._search_all_split("DiagnosticReport", base_params)
        self._cache_included(matches + included)

        reports_by_id = {
            r["id"]: r for r in (matches + included)
            if r.get("resourceType") == "DiagnosticReport" and r.get("id") and self._is_bcrabl_report(r)
        }
        return list(reports_by_id.values())

    def order_for_report(self, report):
        """Resolve the ServiceRequest a DiagnosticReport's `basedOn` points
        at, if any (basedOn can reference other resource types per spec,
        but this IG only ever uses it for the originating ServiceRequest).
        Returns None if there's no basedOn, or it doesn't resolve to a
        ServiceRequest."""
        for ref in report.get("basedOn", []):
            resource = self.resolve_reference(ref)
            if resource and resource.get("resourceType") == "ServiceRequest":
                return resource
        return None

    @staticmethod
    def get_presented_form(report, index=0):
        forms = report.get("presentedForm", [])
        if index < 0 or index >= len(forms):
            return None
        return forms[index]

    def fetch_attachment_bytes(self, attachment):
        """
        Resolve a FHIR Attachment (as used in DiagnosticReport.presentedForm)
        to raw bytes + content type.

        Attachments here are either inlined as base64 in `.data`, or point
        via `.url` at a **FHIR Binary resource** (e.g. "Binary/abc123") —
        not a plain static file. We request it as FHIR JSON
        (`Accept: application/fhir+json`), which reliably returns a Binary
        resource — a JSON object with `contentType` and base64-encoded
        `data` — and decode that. If a server ignores the Accept header and
        returns raw bytes instead, we fall back to using those directly.
        """
        if attachment.get("data"):
            content_type = attachment.get("contentType", "application/octet-stream")
            return base64.b64decode(attachment["data"]), content_type

        url = attachment.get("url")
        if not url:
            return None, None
        full_url = url if url.startswith("http") else f"{self.base_url}/{url.lstrip('/')}"

        resp = requests.get(
            full_url,
            headers={"Accept": "application/fhir+json"},
            auth=self._auth(), verify=self.verify_ssl, timeout=30,
        )
        resp.raise_for_status()

        ctype_header = resp.headers.get("Content-Type", "")
        if "json" in ctype_header or "fhir" in ctype_header:
            binary_resource = resp.json()
            data_b64 = binary_resource.get("data")
            if not data_b64:
                return None, None
            content_type = binary_resource.get("contentType") or attachment.get("contentType", "application/octet-stream")
            return base64.b64decode(data_b64), content_type

        # Server ignored the Accept header and returned raw bytes directly.
        content_type = ctype_header or attachment.get("contentType", "application/octet-stream")
        return resp.content, content_type

    # ---- Stats: system-wide date-range queries -------------------------

    def orders_in_range(self, start_date, end_date):
        """All genomic test orders authored within [start_date, end_date]
        (ISO dates), across all patients — used by the daily stats screen.

        Bundles each order's patient and requester via `_include` (+
        `_include:iterate` for the Practitioner/Organization behind a
        PractitionerRole requester) and seeds the reference cache from
        them (_cache_included()). app.stats() calls patient_for() and
        order_organisation_resource() once per row; without this, every
        one of those was a separate uncached resolve_reference() GET — an
        N+1 that was slow enough to time the page out on a real week of
        data. Same try-categorized-then-fall-back pattern used throughout
        this file (see _active_orders_with_intent, _search_ctdna_service_requests)."""
        params = {
            "authored": [f"ge{start_date}", f"le{end_date}"],
            "_count": 100,
            "_include": ["ServiceRequest:patient", "ServiceRequest:requester"],
            "_include:iterate": SERVICE_REQUEST_ITERATE_INCLUDES,
        }
        try:
            matches, included = self._search_all_split(
                "ServiceRequest", {**params, "category": SERVICE_REQUEST_CATEGORY})
        except requests.HTTPError:
            matches, included = [], []
        if not matches:
            matches, included = self._search_all_split("ServiceRequest", params)
        self._cache_included(included)
        return matches

    def reports_in_range(self, start_date, end_date):
        """All genomic test reports dated within [start_date, end_date]
        (ISO dates), across all patients — used by the daily stats screen.

        Bundles each report's patient, performer, and originating
        ServiceRequest (via `_include=DiagnosticReport:based-on`) plus
        that order's requester one hop further (`_include:iterate`, same
        Practitioner/Organization chain as orders_in_range() above), and
        seeds the reference cache from them. app.stats() calls
        patient_for(), report_organisation(), order_for_report(), and
        order_organisation_resource() once per row — same N+1 concern as
        orders_in_range(), just with more references per row."""
        params = {
            "date": [f"ge{start_date}", f"le{end_date}"],
            "_count": 100,
            "_include": ["DiagnosticReport:patient", "DiagnosticReport:performer", "DiagnosticReport:based-on"],
            "_include:iterate": ["ServiceRequest:requester"] + SERVICE_REQUEST_ITERATE_INCLUDES,
        }
        try:
            matches, included = self._search_all_split(
                "DiagnosticReport", {**params, "category": DIAGNOSTIC_REPORT_CATEGORY})
        except requests.HTTPError:
            matches, included = [], []
        if not matches:
            matches, included = self._search_all_split("DiagnosticReport", params)
        self._cache_included(included)
        return matches

    # ---- ctDNA summary: system-wide, completed bucket date-bound -------

    #: Best-effort text match for ctDNA (circulating tumour DNA) genomic
    #: test orders. This IG doesn't have one confirmed Genomic Test
    #: Directory / SNOMED code specifically for ctDNA testing, so this
    #: matches on the order's code text instead of an exact code — if your
    #: server's ctDNA tests use a consistent `code.coding[].code`, swap
    #: _is_ctdna_order() below for an exact code check, which is more
    #: reliable than text matching.
    CTDNA_TEXT_MATCHES = (
        "ctdna", "circulating tumour dna", "circulating tumor dna",
        "circulating cell-free dna", "cell-free dna", "cfdna",
    )

    #: Confirmed Genomic Test Directory code(s) for ctDNA testing (e.g.
    #: M4.14). Checked *in addition to* CTDNA_TEXT_MATCHES above, not
    #: instead of it — this is the more reliable signal where it's
    #: present, but we don't have a fully exhaustive confirmed code list,
    #: so orders that only match by text (an older/different code, or a
    #: server that doesn't populate GENOMIC_TEST_DIRECTORY_SYSTEM
    #: consistently) still need to keep matching. Add more codes here as
    #: they're confirmed.
    CTDNA_TEST_DIRECTORY_CODES = ("M4.14", "M3.13", "M226.7")

    @classmethod
    def _is_ctdna_order(cls, order):
        if cls.test_directory_code(order.get("code")) in cls.CTDNA_TEST_DIRECTORY_CODES:
            return True
        text = (cls._code_text(order.get("code")) or "").lower()
        return any(term in text for term in cls.CTDNA_TEXT_MATCHES)

    @classmethod
    def _ctdna_code_search_value(cls):
        """CTDNA_TEST_DIRECTORY_CODES as a FHIR token-search value (comma-
        joined `system|code` pairs — comma means OR within one search
        param), for the supplementary code-filtered queries in
        ctdna_orders() below."""
        return ",".join(f"{cls.GENOMIC_TEST_DIRECTORY_SYSTEM}|{code}" for code in cls.CTDNA_TEST_DIRECTORY_CODES)

    def _search_ctdna_service_requests(self, extra_params):
        """Shared `_search_all_split("ServiceRequest", ...)` + category-
        filter-then-fallback plumbing for ctdna_orders()'s three
        ServiceRequest queries (outstanding / completed-with-no-report) —
        same try-categorized-then-fall-back pattern used throughout this
        file (see orders_in_range, _active_orders_with_intent).

        No `ServiceRequest:patient` `_include` — dropped to cut down the
        `_include`/`_include:iterate`/`_revinclude` parameter count after
        this query started 413ing on a live server. app.ctdna_summary()
        still resolves each order's patient via patient_for() for the
        Patient column; without the bundled include, resolve_reference()
        just falls back to one GET per distinct patient instead of getting
        them for free in this Bundle — slower on a range with many
        distinct patients, but each is cached for the rest of the process
        once fetched, and a page that loads slower beats one that 413s."""
        params = {
            "_count": 100,
            "_include": ["ServiceRequest:specimen", "ServiceRequest:requester"],
            "_revinclude": "DiagnosticReport:based-on",
            "_include:iterate": SERVICE_REQUEST_ITERATE_INCLUDES + ["DiagnosticReport:specimen"],
            **extra_params,
        }
        try:
            matches, included = self._search_all_split(
                "ServiceRequest", {**params, "category": SERVICE_REQUEST_CATEGORY})
        except requests.HTTPError:
            matches, included = [], []
        if not matches:
            matches, included = self._search_all_split("ServiceRequest", params)
        return matches, included

    def ctdna_orders(self, start=None, end=None):
        """
        All genomic test orders (ServiceRequest) that look like ctDNA tests
        (see _is_ctdna_order), system-wide — for app.ctdna_summary(), which
        shows *outstanding* orders (any status other than "completed") and
        *completed* orders, both bound to [start, end] (inclusive ISO
        dates) when given. Pass start=end=None to leave every bucket
        unbounded (the old, fully system-wide behaviour — fine for a small
        history, but can trip a 413 from the FHIR server on a large one,
        which is why app.ctdna_summary() always passes a range).

        Three separate queries, since a single unbounded one used to pull
        back *every* ctDNA ServiceRequest this server has ever seen (plus
        each one's `_include`/`_revinclude` fan-out) on every page load:

          1. Outstanding orders (`status` anything but "completed",
             NON_COMPLETED_STATUSES), bound by `authored` to [start, end]
             like the other two buckets. This used to be unconditionally
             unbounded ("show every outstanding order regardless of age")
             — deliberately, since a long-outstanding order is exactly
             what this screen is meant to surface — but on a live server
             with a large backlog of long-lived non-completed
             ServiceRequests, an unbounded outstanding query turned out to
             be exactly the same result-set-too-large 413 this function
             was already rewritten once to avoid for the completed
             bucket. A very old still-active order now only shows up if
             it falls in the selected range.
          2. Completed orders that have a linked report issued within
             [start, end] — queried from the *DiagnosticReport* side
             (bound by its own `date`/`issued`, then `_include`d back to
             the originating ServiceRequest), since that's the date
             app.ctdna_summary()'s completion_date actually filters by
             when a report resolved — bounding the ServiceRequest side by
             its own `authored` instead would wrongly exclude orders
             placed well before the window but completed within it, which
             defeats the point of a turnaround-time screen.
          3. Completed orders with *no* linked report at all, bound by
             `authored` — app.ctdna_summary()'s fallback when no report
             resolved, so this is the one case query 2 can't cover.

        Buckets 1 and 3 each also run a **supplementary code-filtered
        query** — same status/date bound, plus an explicit `code=`
        restriction to CTDNA_TEST_DIRECTORY_CODES (see
        _ctdna_code_search_value) — pooled in alongside the category-based
        query rather than replacing it. The category-based query should
        already be a superset covering these (category is a broader net
        than one specific code), but this catches a genuinely ctDNA-coded
        order on a server where `category` isn't populated reliably,
        same concern _search_ctdna_service_requests's own
        categorized-then-fallback ladder exists for.

        Query 2 (the DiagnosticReport-side completed-with-report bucket)
        is different: `code` is *required* there, not supplementary — an
        unrestricted `category=Genetics` DiagnosticReport search 413'd on
        a live server, so this bucket doesn't run a separate broad
        variant at all any more, only the categorized-then-fallback pair
        with `code` baked into both. The trade-off: a completed order
        whose *report* doesn't carry a CTDNA_TEST_DIRECTORY_CODES code
        won't be found via this bucket (buckets 1/3 still fall back to
        CTDNA_TEXT_MATCHES for their own ServiceRequest-side searches).

        Each order's specimen, patient, and requester (for managing-
        organisation grouping via order_organisation()) come back in the
        same query via `_include`, and any DiagnosticReport whose `basedOn`
        points at one of these orders comes back via
        `_revinclude=DiagnosticReport:based-on` (plus that report's own
        specimen via `_include:iterate`, in case a server attaches the
        specimen to the report rather than the order, and the Practitioner/
        Organization behind a PractitionerRole requester, one hop further).
        Each query paginates up to `_search_all_split`'s default cap (1,000
        records) — see README for what to do if this ever hits that.

        Returns (orders, reports_by_order_id): `orders` is the filtered
        ctDNA ServiceRequest list; `reports_by_order_id` maps a
        ServiceRequest id to its most-recently-issued linked
        DiagnosticReport (there should usually be at most one, but this
        picks the latest if a reflex/repeat test produced more).

        Both are built by filtering on `resourceType` across every resource
        every query returned (matches + included, pooled together) rather
        than trusting Bundle.entry.search.mode to have sorted "match"
        from "include" correctly — some servers don't reliably set
        search.mode on _include/_revinclude'd entries, which would
        otherwise misfile a linked DiagnosticReport as if it were an order
        (with none of the ServiceRequest's own fields) and leave
        reports_by_order_id empty. Duplicate resources across queries
        (e.g. a report picked up by both query 2 and its order's
        `_revinclude` in query 3) are harmless — both dicts below key by
        resource id, so a repeat just overwrites itself.
        """
        all_resources = []

        outstanding_params = {"status": NON_COMPLETED_STATUSES}
        if start and end:
            # Bounded by `authored` like the other two queries — this
            # used to be unconditionally unbounded ("show every
            # outstanding order regardless of age"), but on a server
            # with a large backlog of long-lived non-completed
            # ServiceRequests that's the same unbounded-result-set 413
            # this function was already rewritten once to avoid for the
            # completed bucket. A very old still-active order now only
            # shows up if it falls in the selected range — narrower than
            # before, but a page that loads beats one that 413s.
            outstanding_params["authored"] = [f"ge{start}", f"le{end}"]
        matches, included = self._search_ctdna_service_requests(outstanding_params)
        all_resources += matches + included

        # Supplementary: explicitly filter by CTDNA_TEST_DIRECTORY_CODES
        # (see _ctdna_code_search_value) on top of the category-based
        # query above. The category query is already a superset that
        # should include these — this exists for a server where
        # `category` isn't populated reliably on every ServiceRequest
        # (the same concern _search_ctdna_service_requests's own
        # categorized-then-fallback ladder exists for, just via a
        # different search parameter), so a genuinely ctDNA-coded order
        # still gets found even if its category is missing/wrong. Pooled
        # into all_resources like everything else — duplicates are
        # harmless (orders_by_id below keys by id).
        matches, included = self._search_ctdna_service_requests(
            {**outstanding_params, "code": self._ctdna_code_search_value()})
        all_resources += matches + included

        if start and end:
            # `code` is required here (not supplementary) — this query
            # used to be category-only, which is exactly what 413'd on a
            # live server: a broad `category=Genetics` DiagnosticReport
            # search with no code restriction at all. DiagnosticReport.code
            # is bound to the same Genomic Test Directory value set as
            # ServiceRequest.code in this IG (see
            # GENOMIC_TEST_DIRECTORY_SYSTEM), so restricting by it here is
            # just as valid as on the ServiceRequest side, and keeps this
            # query's result set small regardless of how large this
            # server's overall Genetics-category report volume is. The
            # trade-off: a completed order whose *report* doesn't carry
            # one of CTDNA_TEST_DIRECTORY_CODES won't be found via this
            # bucket any more (buckets 1/3 on the ServiceRequest side still
            # have the CTDNA_TEXT_MATCHES fallback for that case).
            report_params = {
                "_count": 100,
                "date": [f"ge{start}", f"le{end}"],
                "code": self._ctdna_code_search_value(),
                "_include": ["DiagnosticReport:based-on", "DiagnosticReport:specimen"],
                # No ServiceRequest:patient here either — see
                # _search_ctdna_service_requests() for why.
                "_include:iterate": (
                    ["ServiceRequest:specimen", "ServiceRequest:requester"]
                    + SERVICE_REQUEST_ITERATE_INCLUDES
                ),
            }
            try:
                r_matches, r_included = self._search_all_split(
                    "DiagnosticReport", {**report_params, "category": DIAGNOSTIC_REPORT_CATEGORY})
            except requests.HTTPError:
                r_matches, r_included = [], []
            if not r_matches:
                r_matches, r_included = self._search_all_split("DiagnosticReport", report_params)
            all_resources += r_matches + r_included

            completed_params = {"status": "completed", "authored": [f"ge{start}", f"le{end}"]}
            matches, included = self._search_ctdna_service_requests(completed_params)
            all_resources += matches + included
            # Supplementary code-filtered query, same reasoning as the
            # outstanding bucket above.
            matches, included = self._search_ctdna_service_requests(
                {**completed_params, "code": self._ctdna_code_search_value()})
            all_resources += matches + included
        else:
            completed_params = {"status": "completed"}
            matches, included = self._search_ctdna_service_requests(completed_params)
            all_resources += matches + included
            matches, included = self._search_ctdna_service_requests(
                {**completed_params, "code": self._ctdna_code_search_value()})
            all_resources += matches + included

        self._cache_included(all_resources)

        orders_by_id = {
            o["id"]: o for o in all_resources
            if o.get("resourceType") == "ServiceRequest" and o.get("id") and self._is_ctdna_order(o)
        }
        orders = list(orders_by_id.values())

        reports_by_order_id = {}
        for resource in all_resources:
            if resource.get("resourceType") != "DiagnosticReport":
                continue
            for ref in resource.get("basedOn", []):
                ref_str = ref.get("reference", "")
                if not ref_str.startswith("ServiceRequest/"):
                    continue
                order_id = ref_str.split("/", 1)[1]
                existing = reports_by_order_id.get(order_id)
                issued = resource.get("issued") or resource.get("effectiveDateTime") or ""
                existing_issued = (existing.get("issued") or existing.get("effectiveDateTime") or "") if existing else ""
                if existing is None or issued > existing_issued:
                    reports_by_order_id[order_id] = resource

        return orders, reports_by_order_id

    @staticmethod
    def _code_text(codeable_concept):
        """Minimal CodeableConcept -> text, for use in aggregation (the fuller
        version used for on-screen display lives in app.py as a Jinja filter)."""
        if not codeable_concept:
            return None
        if codeable_concept.get("text"):
            return codeable_concept["text"]
        codings = codeable_concept.get("coding", [])
        if codings:
            return codings[0].get("display") or codings[0].get("code")
        return None

    def order_organisation(self, order):
        """Requesting organisation for a ServiceRequest — same reference chain
        as requester_display(), but organisation name only (no practitioner)."""
        requester_ref = order.get("requester")
        if not requester_ref:
            return None
        resource = self.resolve_reference(requester_ref)
        if resource is None:
            return requester_ref.get("display")
        rtype = resource.get("resourceType")
        if rtype == "Organization":
            return resource.get("name") or requester_ref.get("display")
        if rtype == "PractitionerRole":
            org_ref = resource.get("organization")
            if org_ref:
                org = self.resolve_reference(org_ref)
                if org:
                    return org.get("name")
        return requester_ref.get("display")

    def order_organisation_resource(self, order):
        """
        Resolve ServiceRequest.requester down to the Organization resource
        itself (not just its name) — same reference chain as
        order_organisation()/requester_display(): either the requester *is*
        an Organization, or it's a PractitionerRole whose `.organization`
        points at one. Returns None if the requester is missing,
        unresolvable, or neither shape. Used by the ctDNA summary screen,
        which also wants the Organization's ODS code alongside its name
        (see organisation_ods_code()).
        """
        requester_ref = order.get("requester")
        if not requester_ref:
            return None
        resource = self.resolve_reference(requester_ref)
        if resource is None:
            return None
        rtype = resource.get("resourceType")
        if rtype == "Organization":
            return resource
        if rtype == "PractitionerRole":
            org_ref = resource.get("organization")
            if org_ref:
                return self.resolve_reference(org_ref)
        return None

    def order_organisation_ods(self, order):
        """
        ODS code for a ServiceRequest's requesting organisation — same
        requester chain as order_organisation_resource(), but via
        resolve_organisation_ods() instead of organisation_ods_code() so a
        PractitionerRole's `.organization` (unambiguously Organization-typed)
        still yields an ODS code if it's an inline `.identifier` reference
        rather than a literal `.reference` (see medical_record_numbers()).
        order_organisation_resource() alone would return None in that case,
        losing the ODS code even though it's present on the reference.
        """
        requester_ref = order.get("requester")
        if not requester_ref:
            return None
        resource = self.resolve_reference(requester_ref)
        if resource is None:
            return None
        rtype = resource.get("resourceType")
        if rtype == "Organization":
            return self.organisation_ods_code(resource)
        if rtype == "PractitionerRole":
            org_ref = resource.get("organization")
            if org_ref:
                return self.resolve_organisation_ods(org_ref)
        return None

    #: NHS ODS (Organisation Data Service) identifier system — the standard
    #: FHIR identifier system for an NHS organisation's short ODS code (e.g.
    #: "RW3" for a trust). Not confirmed against this specific server; if an
    #: Organization's ODS code lives under a different/no system value,
    #: organisation_ods_code() falls back to the first system-less
    #: identifier it finds.
    ODS_ORGANIZATION_CODE_SYSTEM = "https://fhir.nhs.uk/Id/ods-organization-code"

    @classmethod
    def organisation_ods_code(cls, organisation):
        """The NHS ODS code from an Organization resource's `identifier`
        list, or None if it has no identifiers (or none matching, per the
        fallback above)."""
        if not organisation:
            return None
        identifiers = organisation.get("identifier", [])
        for ident in identifiers:
            if ident.get("system") == cls.ODS_ORGANIZATION_CODE_SYSTEM:
                return ident.get("value")
        for ident in identifiers:
            if not ident.get("system"):
                return ident.get("value")
        return None

    def organizations_without_name(self):
        """
        Every Organization resource on this FHIR server (system-wide, no
        date bound) with no `.name` — the population
        scripts/fix_organization_names.py corrects. Paginates via
        _search_all(), same 1,000-record default cap as other system-wide
        queries (raise max_pages there if a server has more).
        """
        orgs = self._search_all("Organization", {"_count": 100})
        return [o for o in orgs if not o.get("name")]

    #: NHS ODS lookup API (Organisation Reference Data v2.0.0) — a plain
    #: JSON REST API (not FHIR-shaped), open access per NHS Digital's API
    #: catalogue: no API key or onboarding required. The older FHIR-shaped
    #: "directory.spineservices.nhs.uk/STU3/Organization" endpoint some
    #: older docs reference has been retired — this is the current one.
    ODS_LOOKUP_API_URL = "https://directory.spineservices.nhs.uk/ORD/2-0-0/organisations"

    @classmethod
    def ods_lookup_name(cls, ods_code):
        """
        The official organisation name for an ODS code, from the NHS ODS
        lookup API (see ODS_LOOKUP_API_URL), or None if the code is
        invalid/unknown, or the API can't be reached (network error,
        timeout, non-2xx response) — a failed lookup should be reported by
        the caller as "couldn't resolve", not raise. The name is returned
        exactly as ODS has it (upper case, per ODS convention) rather than
        re-cased, since it's the authoritative source for this field.
        """
        if not ods_code:
            return None
        try:
            resp = requests.get(f"{cls.ODS_LOOKUP_API_URL}/{quote(ods_code)}", timeout=10)
            if resp.ok:
                return (resp.json().get("Organisation") or {}).get("Name")
        except requests.RequestException:
            pass
        return None

    def update_organization_name(self, organization, name):
        """
        PUT-updates an Organization resource's `name` on this FHIR server,
        preserving every other field — used by
        scripts/fix_organization_names.py to backfill a name derived from
        an NHS ODS lookup (ods_lookup_name()) for an Organization that has
        none. Raises requests.HTTPError on failure (via _put()).
        """
        updated = dict(organization)
        updated["name"] = name
        return self._put(f"Organization/{organization['id']}", updated)

    def resolve_organisation_ods(self, org_ref):
        """
        ODS code for an Organization reached via a Reference `org_ref`
        (Patient.managingOrganization, PractitionerRole.organization,
        identifier.assigner — anywhere the FHIR profile guarantees the
        target is an Organization). Tries resolving it to a full resource
        and reading organisation_ods_code() first; if that fails (no
        literal `.reference`, or the server doesn't have/expose that
        resource), falls back to the Reference's own inline `.identifier` —
        a logical reference some servers use instead of a literal
        `.reference`, carrying the ODS code directly as
        `identifier.value` (first seen on identifier.assigner in
        medical_record_numbers(); same shape can appear on any
        Organization-typed Reference).

        Only call this where the Reference is unambiguously Organization-
        typed per the FHIR profile — for a Reference that could point at
        several resource types (e.g. ServiceRequest.requester,
        Patient.generalPractitioner directly), an unresolvable inline
        `.identifier` can't be safely assumed to be an ODS code.
        """
        if not org_ref:
            return None
        ods = None
        org = self.resolve_reference(org_ref)
        if org:
            ods = self.organisation_ods_code(org)
        if not ods:
            inline_identifier = org_ref.get("identifier") or {}
            if not inline_identifier.get("system") or inline_identifier.get("system") == self.ODS_ORGANIZATION_CODE_SYSTEM:
                ods = inline_identifier.get("value")
        return ods

    @staticmethod
    def organisation_postcode(organisation):
        """First postcode found on an Organization resource's `address`
        list (Address.postalCode) — used to place a requesting organisation
        on the /stats map via geocode_postcode(). Not confirmed whether
        this server populates Organization.address at all; returns None if
        it doesn't, or has no postalCode on any entry."""
        if not organisation:
            return None
        for address in organisation.get("address", []):
            postcode = address.get("postalCode")
            if postcode:
                return postcode
        return None

    def geocode_postcode(self, postcode):
        """
        (latitude, longitude) for a UK postcode via the free postcodes.io
        API (https://postcodes.io — no API key required), or None if the
        postcode is invalid, unrecognised, or the API can't be reached
        (network error, timeout, non-2xx response) — geocoding failure
        should degrade the /stats map (that organisation just doesn't get a
        marker), not break the whole page. Cached per normalised postcode
        for the life of the process, since the same handful of requesting
        organisations recur across a date range and postcodes.io has no
        reason to be re-queried for one we've already resolved.
        """
        if not postcode:
            return None
        key = postcode.strip().upper()
        if not key:
            return None
        if key in self._geocode_cache:
            return self._geocode_cache[key]
        result = None
        try:
            resp = requests.get(
                f"https://api.postcodes.io/postcodes/{quote(key)}", timeout=5,
            )
            if resp.ok:
                body = resp.json().get("result") or {}
                lat, lon = body.get("latitude"), body.get("longitude")
                if lat is not None and lon is not None:
                    result = (lat, lon)
        except requests.RequestException:
            result = None
        self._geocode_cache[key] = result
        return result

    def organisation_geocode(self, organisation):
        """(latitude, longitude) for an Organization resource, via its
        postcode (organisation_postcode()) and geocode_postcode() — or None
        if it has no address/postcode, or the postcode couldn't be
        geocoded."""
        postcode = self.organisation_postcode(organisation)
        return self.geocode_postcode(postcode) if postcode else None

    #: ONS Open Geography Portal's public ArcGIS FeatureServer for
    #: "Integrated Care Boards (April 2023) EN BGC" (Generalised, Clipped
    #: boundaries) — no API key required. outSR=4326 reprojects from the
    #: source British National Grid (EPSG:27700) to WGS84 lat/lon so the
    #: GeoJSON overlays directly on a Plotly/Mapbox map. Only the two
    #: fields the /stats choropleth needs are requested: ICB23NM (official
    #: name, e.g. "NHS Greater Manchester Integrated Care Board") and
    #: ICB23CD (ONS area code, used as the choropleth's join key).
    ICB_BOUNDARY_GEOJSON_URL = (
        "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
        "Integrated_Care_Boards_April_2023_EN_BGC/FeatureServer/0/query"
        "?where=1%3D1&outFields=ICB23NM,ICB23CD&outSR=4326&f=geojson"
    )

    #: Class-level (not per-instance) cache: this is static reference data —
    #: NHS ICB boundaries don't depend on which FHIR server/credentials a
    #: given FhirClient instance was built with — so it's fetched once per
    #: process rather than once per instance.
    _icb_boundary_cache = None

    @classmethod
    def fetch_icb_boundaries(cls):
        """
        GeoJSON FeatureCollection of NHS England Integrated Care Board (ICB)
        boundaries (see ICB_BOUNDARY_GEOJSON_URL), for the /stats "Orders by
        patient ICS" choropleth. Cached at class level for the process
        lifetime — ~42 polygons, not worth re-fetching per request.

        Returns None if the ONS ArcGIS service can't be reached or returns
        no features — a failed fetch is *not* cached, so the next call
        retries rather than permanently giving up for the process's
        lifetime (unlike geocode_postcode()'s per-postcode caching, this
        isn't about an individual invalid input, just transient network
        conditions); callers should degrade to "no map" rather than
        failing the whole /stats page.
        """
        if cls._icb_boundary_cache is not None:
            return cls._icb_boundary_cache
        try:
            resp = requests.get(cls.ICB_BOUNDARY_GEOJSON_URL, timeout=15)
            if resp.ok:
                geojson = resp.json()
                if geojson.get("features"):
                    cls._icb_boundary_cache = geojson
        except requests.RequestException:
            pass
        return cls._icb_boundary_cache

    @staticmethod
    def _point_in_ring(lon, lat, ring):
        """Ray-casting point-in-polygon test for a single GeoJSON linear
        ring ([lon, lat] pairs) — standard even-odd crossing count, no
        external geometry library needed for the one shape test
        icb_for_coordinates() below requires."""
        inside = False
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if ((yi > lat) != (yj > lat)) and (
                lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
            ):
                inside = not inside
            j = i
        return inside

    @classmethod
    def _point_in_geometry(cls, lon, lat, geometry):
        """True if (lon, lat) falls inside a GeoJSON Polygon/MultiPolygon
        geometry — outer ring must contain the point and no hole (any ring
        after the first) may."""
        gtype = geometry.get("type")
        coords = geometry.get("coordinates") or []
        polygons = coords if gtype == "MultiPolygon" else [coords] if gtype == "Polygon" else []
        for polygon in polygons:
            if not polygon or not cls._point_in_ring(lon, lat, polygon[0]):
                continue
            if any(cls._point_in_ring(lon, lat, hole) for hole in polygon[1:]):
                continue
            return True
        return False

    @classmethod
    def icb_for_coordinates(cls, lat, lon):
        """Official ICB name (ICB23NM) whose boundary polygon
        (fetch_icb_boundaries()) contains (lat, lon), via a simple
        point-in-polygon test — or None if boundary data couldn't be
        fetched, or the point doesn't fall inside any ICB polygon (e.g.
        outside England/Wales, or just off a coastal/boundary edge)."""
        boundaries = cls.fetch_icb_boundaries()
        if not boundaries:
            return None
        for feature in boundaries.get("features", []):
            if cls._point_in_geometry(lon, lat, feature.get("geometry") or {}):
                return feature.get("properties", {}).get("ICB23NM")
        return None

    def organisation_ics(self, organisation):
        """The Integrated Care System a requesting Organization resource's
        address falls within, via organisation_geocode() (postcode ->
        lat/lon) + icb_for_coordinates() (lat/lon -> ICB polygon) — used for
        the /stats "by requesting organisation's ICS" choropleths in place
        of the patient's own managingOrganization (which reflects where the
        *patient* is registered, not where the order was *placed from*).
        Returns None if the organisation has no resolvable postcode, or the
        geocoded point doesn't fall inside any ICB polygon. Cached per
        Organization id for the process lifetime — the same handful of
        requesting organisations recur across a date range."""
        if not organisation:
            return None
        key = organisation.get("id")
        if key and key in self._org_ics_cache:
            return self._org_ics_cache[key]
        geocode = self.organisation_geocode(organisation)
        result = self.icb_for_coordinates(geocode[0], geocode[1]) if geocode else None
        if key:
            self._org_ics_cache[key] = result
        return result

    #: iGene report identifier system (NW Genomics IG-specific) — a local
    #: cross-reference id, e.g. into the iGene LIMS, that may be carried on
    #: either the ServiceRequest or the DiagnosticReport depending on the
    #: server, hence checking both in igene_report_identifier() below.
    IGENE_REPORT_IDENTIFIER_SYSTEM = "https://fhir.nwgenomics.nhs.uk/iGene/ReportIdentifier"

    #: iGene specimen identifier system (NW Genomics IG-specific), carried on
    #: the Specimen resource's own `identifier` list.
    SPECIMEN_IDENTIFIER_SYSTEM = "https://fhir.nwgenomics.nhs.uk/iGene/SpecimenIdentifier"

    #: HL7 v2-0203 identifier-type system, used to tell a placer order number
    #: ("PLAC", assigned by the ordering/requesting system) apart from a
    #: filler order number ("FILL", assigned by the fulfilling/lab system) on
    #: ServiceRequest.identifier.
    IDENTIFIER_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v2-0203"
    PLACER_IDENTIFIER_TYPE = "PLAC"
    FILLER_IDENTIFIER_TYPE = "FILL"

    #: UK Core identifier systems for a requesting clinician's professional
    #: registration number, carried on the Practitioner resource behind a
    #: ServiceRequest.requester PractitionerRole. GMC (General Medical
    #: Council) is checked first; GMP (General Medical Practitioner code)
    #: is a fallback for requesters who carry that instead. Not confirmed
    #: against a real server — if the code never appears in brackets next
    #: to a requesting clinician's name, sample a real Practitioner.identifier
    #: array and check the system URI actually used.
    GMC_NUMBER_SYSTEM = "https://fhir.hl7.org.uk/Id/gmc-number"
    GMP_NUMBER_SYSTEM = "https://fhir.hl7.org.uk/Id/gmp-number"

    @staticmethod
    def _identifier_value(resource, system):
        """First identifier value on `resource` whose `system` matches, or
        None if `resource` is falsy or has no matching identifier."""
        if not resource:
            return None
        for ident in resource.get("identifier", []):
            if ident.get("system") == system:
                return ident.get("value")
        return None

    @classmethod
    def igene_report_identifier(cls, order, report):
        """The iGene report identifier (see IGENE_REPORT_IDENTIFIER_SYSTEM),
        checked on the order first, then the report (`report` may be None) —
        either resource might carry it depending on how a server populates
        this IG-specific identifier."""
        return (cls._identifier_value(order, cls.IGENE_REPORT_IDENTIFIER_SYSTEM)
                or cls._identifier_value(report, cls.IGENE_REPORT_IDENTIFIER_SYSTEM))

    @classmethod
    def report_identifier(cls, report):
        """The iGene report identifier carried directly on a DiagnosticReport
        (see IGENE_REPORT_IDENTIFIER_SYSTEM). Unlike igene_report_identifier()
        above, this doesn't fall back to a linked order — used where only the
        report itself is in scope (the patient page's report list)."""
        return cls._identifier_value(report, cls.IGENE_REPORT_IDENTIFIER_SYSTEM)

    @classmethod
    def specimen_identifier(cls, specimen):
        """The iGene specimen identifier (see SPECIMEN_IDENTIFIER_SYSTEM)."""
        return cls._identifier_value(specimen, cls.SPECIMEN_IDENTIFIER_SYSTEM)

    #: England's National Genomic Test Directory CodeSystem — the value set
    #: ServiceRequest.code/DiagnosticReport.code are bound to in this IG (see
    #: module docstring). A CodeableConcept here can carry more than one
    #: coding (e.g. an NGTD code alongside a local/SNOMED one), so
    #: test_directory_code() below picks the coding by this system
    #: specifically rather than assuming coding[0] is the right one.
    GENOMIC_TEST_DIRECTORY_SYSTEM = "https://fhir.nhs.uk/CodeSystem/England-GenomicTestDirectory"

    @classmethod
    def test_directory_code(cls, codeable_concept):
        """The Genomic Test Directory code from a ServiceRequest.code/
        DiagnosticReport.code CodeableConcept — i.e. the coding whose
        `system` is GENOMIC_TEST_DIRECTORY_SYSTEM, ignoring any other
        coding (or `.text`/`.display`) the CodeableConcept might also carry.
        Returns None if there's no coding with that system at all."""
        if not codeable_concept:
            return None
        for coding in codeable_concept.get("coding", []):
            if coding.get("system") == cls.GENOMIC_TEST_DIRECTORY_SYSTEM:
                return coding.get("code")
        return None

    @staticmethod
    def _identifier_by_type_full(resource, type_code):
        """Full Identifier dict (not just its value) from `resource.identifier`
        whose `.type.coding` includes the given v2-0203 code (see
        IDENTIFIER_TYPE_SYSTEM/PLACER_IDENTIFIER_TYPE/FILLER_IDENTIFIER_TYPE)
        — the standard FHIR way of distinguishing a placer order number from
        a filler order number on the same ServiceRequest. Kept separate from
        _identifier_by_type() below so callers that only want the value
        aren't forced to unpack a dict, while placer_identifier_assigner()
        can still get at `.assigner`."""
        if not resource:
            return None
        for ident in resource.get("identifier", []):
            codings = (ident.get("type") or {}).get("coding", [])
            if any(c.get("code") == type_code for c in codings):
                return ident
        return None

    @classmethod
    def _identifier_by_type(cls, resource, type_code):
        """Identifier value from `resource.identifier` whose `.type.coding`
        includes the given v2-0203 code — see _identifier_by_type_full()."""
        ident = cls._identifier_by_type_full(resource, type_code)
        return ident.get("value") if ident else None

    @classmethod
    def placer_identifier(cls, order):
        """Placer order number (HL7 v2-0203 code "PLAC") — the identifier
        assigned by the ordering/requesting system."""
        return cls._identifier_by_type(order, cls.PLACER_IDENTIFIER_TYPE)

    @classmethod
    def filler_identifier(cls, order):
        """Filler order number (HL7 v2-0203 code "FILL") — the identifier
        assigned by the fulfilling/lab system."""
        return cls._identifier_by_type(order, cls.FILLER_IDENTIFIER_TYPE)

    #: HL7 v2-0203 identifier-type codes for the two Hospital Spell
    #: Identifier flavours NHS England's guidance describes — "AN"
    #: (Account Number) and "VN" (Visit Number). A real example
    #: (ServiceRequest/5743) carries system
    #: http://terminology.hl7.org/CodeSystem/v2-0203, code "VN".
    HOSPITAL_SPELL_IDENTIFIER_TYPES = {"AN": "Account number", "VN": "Visit number"}

    @classmethod
    def _identifier_type_label(cls, identifier):
        """Human-readable label for an Identifier's `.type` — "Account
        number"/"Visit number" for the AN/VN codes above, else that
        type's own `.text`/coding `.display` if it has one, else None."""
        type_cc = identifier.get("type") or {}
        for coding in type_cc.get("coding", []):
            label = cls.HOSPITAL_SPELL_IDENTIFIER_TYPES.get(coding.get("code"))
            if label:
                return label
        return type_cc.get("text") or next(
            (c.get("display") for c in type_cc.get("coding", []) if c.get("display")), None)

    def _format_spell_identifier(self, identifier):
        """"value (label — assigner)" for a Hospital Spell identifier —
        label from _identifier_type_label() (e.g. "Visit number"),
        assigner from _resolve_assigner_display() on the identifier's own
        `.assigner` (same Reference-or-logical-reference resolution
        placer_identifier_assigner()/medical_record_numbers() use).
        Either half can be missing independently; falls all the way back
        to the bare value if neither resolved. None if there's no value
        at all."""
        value = identifier.get("value")
        if not value:
            return None
        label = self._identifier_type_label(identifier)
        assigner = self._resolve_assigner_display(identifier.get("assigner"))
        if label and assigner:
            return f"{value} ({label} — {assigner})"
        if label or assigner:
            return f"{value} ({label or assigner})"
        return value

    def hospital_spell_identifier(self, resource):
        """The Hospital Spell Identifier (NHS England's term for a hospital
        provider spell / visit number — some trusts, e.g. Liverpool
        Women's and Alder Hey, populate this as an account number),
        annotated with which kind it is where known (e.g. "1001166717
        (Visit number)").

        Prefers the *inline* identifier on ServiceRequest.encounter /
        DiagnosticReport.encounter — a FHIR *logical* reference
        (Reference.identifier, a value inlined on the reference itself),
        so no follow-up GET needed. Falls back to resolving the
        referenced Encounter resource itself (`.encounter.reference`,
        via the same resolve_reference() cache every other reference in
        this file uses) when there's no inline identifier, and reading
        *its* `.identifier` list — preferring one typed AN or VN if the
        Encounter has more than one identifier, else its first one.
        Multiple orders/reports from the same hospital stay share this
        same value, so it's how they'd be traced back to one spell.

        Returns None if there's no `encounter` at all, no inline
        identifier, and either no `.reference` to fall back to or the
        referenced Encounter doesn't resolve/has no identifier of its
        own."""
        if not resource:
            return None
        encounter_ref = resource.get("encounter") or {}

        inline_identifier = encounter_ref.get("identifier")
        if inline_identifier:
            formatted = self._format_spell_identifier(inline_identifier)
            if formatted:
                return formatted

        if not encounter_ref.get("reference"):
            return None
        encounter = self.resolve_reference(encounter_ref)
        if not encounter:
            return None

        preferred, fallback = None, None
        for ident in encounter.get("identifier", []):
            if not ident.get("value"):
                continue
            type_codes = [c.get("code") for c in (ident.get("type") or {}).get("coding", [])]
            if any(code in self.HOSPITAL_SPELL_IDENTIFIER_TYPES for code in type_codes):
                preferred = ident
                break
            if fallback is None:
                fallback = ident
        chosen = preferred or fallback
        return self._format_spell_identifier(chosen) if chosen else None

    def hospital_spell_identifiers(self, encounter):
        """Every identifier on an Encounter resource itself (as opposed to
        hospital_spell_identifier()'s single "best" value resolved from a
        ServiceRequest/DiagnosticReport) — in practice mostly Visit
        Number/Account Number (HL7 v2-0203 codes VN/AN), each annotated
        with its type label and assigning organisation the same way (see
        _format_spell_identifier()), but shown as the full list since an
        Encounter can carry more than one. Used by the patient page's
        Hospital Spells table. Returns [] if there's no Encounter or it
        has no identifiers with a value."""
        if not encounter:
            return []
        return [
            text for text in (
                self._format_spell_identifier(ident) for ident in encounter.get("identifier", [])
            ) if text
        ]

    def placer_identifier_assigner(self, order):
        """Assigning-authority display (name and/or ODS code — see
        _name_with_ods()) for the placer order number's `Identifier.assigner`
        — order_view.html shows this next to the placer number, same idea as
        the hospital number's assigner shown next to it (medical_record_
        numbers()'s assigner_name/assigner_ods). Returns None if there's no
        placer identifier, no assigner on it, or the assigner resolves to
        neither a name nor an ODS code."""
        ident = self._identifier_by_type_full(order, self.PLACER_IDENTIFIER_TYPE)
        return self._resolve_assigner_display(ident.get("assigner")) if ident else None

    def order_indication(self, order):
        """Genomic disease / clinical indication for a ServiceRequest, from
        reasonCode (bound to Genomic Clinical Indication Codes in the IG)."""
        labels = [self._code_text(rc) for rc in order.get("reasonCode", [])]
        labels = [l for l in labels if l]
        return "; ".join(labels) if labels else "Unspecified"

    def report_organisation(self, report):
        """Performing organisation for a DiagnosticReport, from `performer`
        (mixed list of Organization/Practitioner/PractitionerRole refs per
        the profile — we pick the first one that resolves to an Organization)."""
        for ref in report.get("performer", []):
            resource = self.resolve_reference(ref)
            if resource and resource.get("resourceType") == "Organization":
                return resource.get("name")
        performers = report.get("performer", [])
        return performers[0].get("display") if performers else None

    def report_indication(self, report):
        """
        Genomic disease / clinical indication for a DiagnosticReport.
        DiagnosticReport itself has no reasonCode, so we follow `basedOn`
        back to the originating ServiceRequest and use its reasonCode —
        the same value set as order_indication(). Falls back to
        `conclusionCode` (the report's own clinical conclusion) if the
        order can't be resolved, though that's a related-but-different
        field (interpretation of results, not indication for testing).
        """
        for ref in report.get("basedOn", []):
            sr = self.resolve_reference(ref)
            if sr and sr.get("resourceType") == "ServiceRequest":
                labels = [self._code_text(rc) for rc in sr.get("reasonCode", [])]
                labels = [l for l in labels if l]
                if labels:
                    return "; ".join(labels)
        labels = [self._code_text(cc) for cc in report.get("conclusionCode", [])]
        labels = [l for l in labels if l]
        return "; ".join(labels) if labels else "Unspecified"

    def results_interpreter_display(self, report):
        """
        Display string for DiagnosticReport.resultsInterpreter — the
        person/organisation who interpreted or authorised the report's
        results (e.g. a reporting clinical scientist or consultant),
        0..* references to Practitioner | PractitionerRole | Organization
        per FHIR R4. Each entry resolves to "Name (Type)": for a
        PractitionerRole, the underlying Practitioner's own name is used
        where it resolves (same drill-down as requester_display()), since a
        bare role reference is rarely meaningful by itself; Type is the
        resolved resource's FHIR resourceType (Practitioner/
        PractitionerRole/Organization), or "Unknown" for a reference that
        couldn't be dereferenced at all. Joined with "; " since a report
        can carry more than one interpreter (e.g. a scientist and an
        authorising consultant). Returns "—" if resultsInterpreter is
        absent or empty, matching requester_display()'s no-data convention.
        """
        entries = []
        for ref in report.get("resultsInterpreter", []):
            resource = self.resolve_reference(ref)
            if resource is None:
                name = ref.get("display") or ref.get("reference") or "Unknown"
                entries.append(f"{name} (Unknown)")
                continue

            rtype = resource.get("resourceType")
            if rtype == "Practitioner":
                name = self._practitioner_name(resource) or ref.get("display") or "Unknown"
            elif rtype == "PractitionerRole":
                practitioner_ref = resource.get("practitioner")
                practitioner = self.resolve_reference(practitioner_ref) if practitioner_ref else None
                name = (practitioner and self._practitioner_name(practitioner)) or ref.get("display") or "Unknown"
            elif rtype == "Organization":
                name = resource.get("name") or ref.get("display") or "Unknown"
            else:
                name = ref.get("display") or resource.get("id") or "Unknown"

            entries.append(f"{name} ({rtype or 'Unknown'})")
        return "; ".join(entries) if entries else "—"

    # ---- Geography: ICS and country from Patient ----------------------

    def patient_for(self, resource):
        """Resolve the Patient a ServiceRequest/DiagnosticReport's `subject`
        (or `patient`) reference points to."""
        ref = resource.get("subject") or resource.get("patient")
        return self.resolve_reference(ref) if ref else None

    def patient_ics(self, patient):
        """The patient's Integrated Care System, from managingOrganization."""
        if not patient:
            return None
        org_ref = patient.get("managingOrganization")
        if not org_ref:
            return None
        org = self.resolve_reference(org_ref)
        if not org:
            return org_ref.get("display")
        return org.get("name") or org_ref.get("display")

    @staticmethod
    def _name_with_ods(name, ods):
        """Combine a resolved name and ODS code as "Name (ODS)", falling
        back to whichever one is available if only one resolved — so an ODS
        code still displays even when the organisation's name couldn't be
        resolved (or vice versa)."""
        if name and ods:
            return f"{name} ({ods})"
        return name or ods

    def _resolve_assigner_display(self, assigner_ref):
        """Name/ODS display (_name_with_ods()) for an Identifier.assigner
        Reference — shared by medical_record_numbers() and
        placer_identifier_assigner(). `assigner` can point at an
        Organization either by literal `.reference` (fetched and passed to
        organisation_ods_code() for its ODS code) or by an inline
        `.identifier` — a logical reference with no resource to fetch, where
        this server puts the ODS code directly as `assigner.identifier.value`;
        resolve_organisation_ods() checks both. Returns None if there's no
        assigner at all, or it resolves to neither a name nor an ODS code."""
        if not assigner_ref:
            return None
        assigner = self.resolve_reference(assigner_ref)
        name = (assigner.get("name") if assigner else None) or assigner_ref.get("display")
        ods = self.resolve_organisation_ods(assigner_ref)
        return self._name_with_ods(name, ods)

    def patient_ics_display(self, patient):
        """
        ICS name with its ODS code appended (see organisation_ods_code()),
        e.g. "NHS Greater Manchester ICB (14L)", for on-screen display.
        managingOrganization is unambiguously an Organization reference, so
        resolve_organisation_ods() can fall back to an inline
        `.identifier` (see medical_record_numbers()) if it's not a literal
        `.reference`. patient_ics() above stays name-only, since it also
        doubles as a /stats grouping key where appending the ODS code would
        fragment the aggregation.
        """
        if not patient:
            return None
        org_ref = patient.get("managingOrganization")
        if not org_ref:
            return None
        org = self.resolve_reference(org_ref)
        name = (org.get("name") if org else None) or org_ref.get("display")
        ods = self.resolve_organisation_ods(org_ref)
        return self._name_with_ods(name, ods)

    _COUNTRY_CODES = {"X24": "England", "W00": "Wales"}

    @classmethod
    def _find_country_code(cls, obj):
        """
        Recursively scan an identifier entry for one of the known country
        codes (X24 = England, W00 = Wales). This IG doesn't put a single
        well-known field on the identifier for this, so rather than assume
        one exact path (e.g. assigner.identifier.value), we check anywhere
        it could plausibly appear — system, value, assigner, extensions —
        and use whichever we find first. Verify against a real Patient
        record and narrow this if it turns out to live somewhere specific.
        """
        if isinstance(obj, dict):
            for v in obj.values():
                found = cls._find_country_code(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = cls._find_country_code(v)
                if found:
                    return found
        elif isinstance(obj, str):
            for code in cls._COUNTRY_CODES:
                if code in obj:
                    return code
        return None

    def patient_country(self, patient):
        """Country (England/Wales) inferred from the NHS number identifier's
        associated code (X24/W00) — see _find_country_code for how/why this
        searches broadly rather than one fixed field."""
        if not patient:
            return None
        for identifier in patient.get("identifier", []):
            code = self._find_country_code(identifier)
            if code:
                return self._COUNTRY_CODES[code]
        return None

    def general_practitioner_display(self, patient):
        """
        Display string for Patient.generalPractitioner — a list of
        references to Practitioner | Organization | PractitionerRole per
        FHIR R4 (the same reference shape as ServiceRequest.requester,
        resolved the same way as requester_display()), each with the GP
        practice's ODS code appended where one resolves — either the
        reference itself if it's an Organization, or a PractitionerRole's
        `.organization` (both unambiguously Organization-typed, so
        resolve_organisation_ods() can fall back to an inline `.identifier`
        — see medical_record_numbers() — if it's not a literal
        `.reference`). A bare Practitioner reference has no organisation to
        pull an ODS code from, so shows name only. _name_with_ods() means an
        ODS code still shows even if the practice name itself didn't
        resolve. Joined with "; " since a patient can have more than one GP
        on record.
        """
        if not patient:
            return None
        names = []
        for ref in patient.get("generalPractitioner", []):
            resource = self.resolve_reference(ref)
            org_ref_for_ods = None
            if resource is None:
                name = ref.get("display") or ref.get("reference")
            else:
                rtype = resource.get("resourceType")
                if rtype == "Practitioner":
                    name = self._practitioner_name(resource) or ref.get("display")
                elif rtype == "Organization":
                    name = resource.get("name") or ref.get("display")
                    org_ref_for_ods = ref
                elif rtype == "PractitionerRole":
                    practitioner_name = None
                    practitioner_ref = resource.get("practitioner")
                    if practitioner_ref:
                        practitioner = self.resolve_reference(practitioner_ref)
                        if practitioner:
                            practitioner_name = self._practitioner_name(practitioner)
                    org_name = None
                    org_ref = resource.get("organization")
                    if org_ref:
                        org = self.resolve_reference(org_ref)
                        if org:
                            org_name = org.get("name")
                        org_ref_for_ods = org_ref
                    name = (f"{practitioner_name} ({org_name})" if practitioner_name and org_name
                            else practitioner_name or org_name or ref.get("display"))
                else:
                    name = ref.get("display") or resource.get("id")
            ods = self.resolve_organisation_ods(org_ref_for_ods) if org_ref_for_ods else None
            entry = self._name_with_ods(name, ods)
            if entry:
                names.append(entry)
        return "; ".join(names) if names else None

    # ---- Specimens -------------------------------------------------

    def resolve_specimens(self, resource):
        """Resolve the Specimen resources a ServiceRequest or DiagnosticReport
        points to via its `specimen` reference list."""
        specimens = []
        for ref in resource.get("specimen", []):
            spec = self.resolve_reference(ref)
            if spec:
                specimens.append(spec)
        return specimens

    def encounters_for(self, resources):
        """Distinct Encounter resources referenced by `resources`' (a
        patient's orders and reports) `.encounter.reference`, resolved
        via resolve_reference()'s cache and deduplicated by Encounter id
        — used by the patient page's Hospital Spell section. A resource
        with no `.encounter`, or one whose reference doesn't resolve
        (deleted/cross-server/etc.), just contributes nothing rather than
        raising."""
        encounters_by_id = {}
        for resource in resources:
            encounter_ref = (resource or {}).get("encounter") or {}
            if not encounter_ref.get("reference"):
                continue
            encounter = self.resolve_reference(encounter_ref)
            if encounter and encounter.get("id"):
                encounters_by_id[encounter["id"]] = encounter
        return list(encounters_by_id.values())

    # ---- Patient data clear-down (destructive) -------------------------

    def clear_down_patient(self, patient_id):
        """
        DELETE every Specimen, DiagnosticReport, ServiceRequest, and
        AuditEvent resource for a patient from the FHIR server —
        irreversible. Meant for resetting a test/demo patient's genomic
        test data (and its audit trail) between runs, not for use against
        real clinical records. Patient and Observation resources are left
        alone — only the resource types the clear-down button is
        documented as deleting are touched.

        Deletes reports and orders before specimens, on the theory that a
        server enforcing referential integrity is more likely to reject
        deleting a Specimen still referenced by a live DiagnosticReport/
        ServiceRequest than the reverse — FHIR doesn't mandate this
        ordering though, so a real server's behaviour here is unverified.
        AuditEvents (fetched via audit_events_for_patient(), no date bound
        — safe unbounded here since it's patient-scoped, not system-wide,
        same reasoning as that method's own docstring) are deleted last,
        since nothing else here references them.

        Returns {"deleted": [...], "failed": [...]}, each a list of
        "ResourceType/id" strings, so the caller can show exactly what
        happened rather than a single pass/fail flag — a partial
        clear-down (e.g. one order the server refuses to delete) is still
        useful information, not a reason to hide everything else that did
        get deleted.
        """
        orders = self.lab_orders_for_patient(patient_id)
        reports = self.lab_reports_for_patient(patient_id)
        audit_events = self.audit_events_for_patient(patient_id)

        specimens_by_id = {}
        for resource in orders + reports:
            for spec in self.resolve_specimens(resource):
                if spec.get("id"):
                    specimens_by_id[spec["id"]] = spec

        deleted, failed = [], []

        def attempt(ref):
            (deleted if self._delete(ref) else failed).append(ref)

        for r in reports:
            if r.get("id"):
                attempt(f"DiagnosticReport/{r['id']}")
        for o in orders:
            if o.get("id"):
                attempt(f"ServiceRequest/{o['id']}")
        for spec_id in specimens_by_id:
            attempt(f"Specimen/{spec_id}")
        for a in audit_events:
            if a.get("id"):
                attempt(f"AuditEvent/{a['id']}")

        return {"deleted": deleted, "failed": failed}

    def clear_down_patient_and_record(self, patient_id):
        """
        Like clear_down_patient(), but also deletes the Patient resource
        itself afterwards — used by the admin screen's bulk test-patient
        clear-down, where (unlike the per-patient "Clear down patient
        data" button on the patient page) removing the Patient record too
        is exactly the point: purging synthetic/test patients entirely,
        not just their genomic test data.
        """
        result = self.clear_down_patient(patient_id)
        ref = f"Patient/{patient_id}"
        (result["deleted"] if self._delete(ref) else result["failed"]).append(ref)
        return result

    #: Inclusive (low, high) integer ranges — parsed from the NHS number
    #: identifier value — conventionally reserved for synthetic/test
    #: patients rather than real ones: "4xx" (400,000,000-499,999,999) and
    #: "6xx"/"7xx" (600,000,000-799,999,999). Used by the admin screen to
    #: find test patients to purge; adjust here if your environment uses
    #: different test-number conventions.
    NHS_NUMBER_TEST_RANGES = [
        (400_000_000, 499_999_999),
        (600_000_000, 799_999_999),
    ]

    @classmethod
    def nhs_number_in_ranges(cls, patient, ranges=None):
        """
        True if `patient`'s NHS number (see nhs_number()) falls within any
        of `ranges` (default NHS_NUMBER_TEST_RANGES: 400,000,000-499,999,999
        and 600,000,000-799,999,999). Non-digit characters (spaces, etc.)
        are stripped before parsing the NHS number to an int, so formatting
        doesn't affect the check. Returns False if there's no NHS number to
        check at all.
        """
        ranges = ranges or cls.NHS_NUMBER_TEST_RANGES
        nhs = cls.nhs_number(patient)
        if not nhs:
            return False
        digits = "".join(ch for ch in nhs if ch.isdigit())
        if not digits:
            return False
        value = int(digits)
        return any(low <= value <= high for low, high in ranges)

    def patients_in_nhs_number_ranges(self, ranges=None):
        """
        Every Patient resource system-wide whose NHS number falls within
        any of `ranges` (see nhs_number_in_ranges()) — used by the admin
        screen to find test/synthetic patients to purge. FHIR identifier
        search is exact-match only (no numeric range support), so this
        needs every Patient and filters client-side — but not via one
        unfiltered system-wide search: a real HealthConnect CDR (this
        app's FHIR_BASE_URL) 413s on that regardless of `_count` or an
        unrelated filter parameter (see "413s on unfiltered system-wide
        searches" in CLAUDE.md). Instead, fetched one organisation at a
        time (`_search_all_by_organization()`, via
        `Patient.managingOrganization`) — organisation-scoped batching
        was the fix confirmed to actually work against this server.

        A Patient with no `managingOrganization` at all wouldn't be
        found by any of those organisation-scoped searches — best-effort
        caught via one extra `organization:missing=true` search
        afterwards (same fallback-modifier pattern
        orphaned_service_requests() uses for `subject:missing`; not
        confirmed this server supports the `:missing` modifier, so a
        failure there is swallowed rather than losing the results
        already gathered per-organisation).
        """
        organizations = self.all_organizations_by_ods().values()
        patients, _ = self._search_all_by_organization("Patient", organizations)
        seen_ids = {p["id"] for p in patients if p.get("id")}
        try:
            unassigned = self._search_all("Patient", {"organization:missing": "true", "_count": 20})
            for p in unassigned:
                if p.get("id") not in seen_ids:
                    patients.append(p)
                    seen_ids.add(p.get("id"))
        except requests.HTTPError:
            pass
        return [p for p in patients if self.nhs_number_in_ranges(p, ranges)]

    def orphaned_service_requests(self):
        """
        Every ServiceRequest resource system-wide with no `subject`
        reference at all — i.e. not associated with any patient — used by
        the admin screen's orphaned-order clear-down. Tries the standard
        `subject:missing=true` search modifier first; not every FHIR
        server supports `:missing` (unverified against this one — see
        README), so this falls back to fetching every ServiceRequest
        system-wide (paginated) and filtering client-side on an absent
        `subject` if the modifier search comes back empty.
        """
        try:
            orders = self._search_all("ServiceRequest", {"subject:missing": "true", "_count": 100})
            if orders:
                return [o for o in orders if not o.get("subject")]
        except requests.HTTPError:
            pass
        orders = self._search_all("ServiceRequest", {"_count": 100})
        return [o for o in orders if not o.get("subject")]

    def _delete_resources(self, resource_type, resources):
        """Shared bulk-delete for a list of resources of the same
        `resource_type` (e.g. "ServiceRequest", "DiagnosticReport").
        Returns {"deleted": [...], "failed": [...]} like
        clear_down_patient(). Used by clear_down_orphaned_service_requests(),
        clear_down_orders_with_unknown_patient(), and
        clear_down_bcrabl_reports_without_components()."""
        deleted, failed = [], []
        for r in resources:
            if not r.get("id"):
                continue
            ref = f"{resource_type}/{r['id']}"
            (deleted if self._delete(ref) else failed).append(ref)
        return {"deleted": deleted, "failed": failed}

    def clear_down_orphaned_service_requests(self):
        """DELETE every ServiceRequest with no `subject` reference (see
        orphaned_service_requests()). Returns {"deleted": [...], "failed":
        [...]} like clear_down_patient()."""
        return self._delete_resources("ServiceRequest", self.orphaned_service_requests())

    def orders_with_unknown_patient(self, orders):
        """
        Filters `orders` (e.g. from active_placer_orders()/
        active_filler_orders()) down to the ones whose patient can't be
        resolved via patient_for() — either `subject` is missing entirely,
        or it's a reference present but not resolvable to an actual
        Patient (deleted, dangling, cross-server, etc.). Broader than
        orphaned_service_requests() (which only catches a wholly absent
        `subject`), since a present-but-broken reference counts as
        "unknown" here too. Used by the test orders screen's "delete
        orders with unknown patient" action.
        """
        return [o for o in orders if self.patient_for(o) is None]

    def clear_down_orders_with_unknown_patient(self, orders):
        """DELETE every order in `orders` whose patient can't be resolved
        (see orders_with_unknown_patient()). Returns {"deleted": [...],
        "failed": [...]} like clear_down_patient()."""
        return self._delete_resources("ServiceRequest", self.orders_with_unknown_patient(orders))

    def bcrabl_reports_without_components(self, reports):
        """
        Filters `reports` (e.g. from bcrabl_reports()) down to the ones
        with no component-level results at all — none of their linked
        Observations (if any) carry a non-empty `component` array. Used by
        the Cepheid Test Results screen's "delete reports with no
        component-level results" action, for reports whose results table
        would show nothing useful anyway (see component_rows() in app.py).
        """
        results = []
        for report in reports:
            observations = self.observations_for_report(report)
            if not any(obs.get("component") for obs in observations):
                results.append(report)
        return results

    def clear_down_bcrabl_reports_without_components(self, reports):
        """DELETE every report in `reports` with no component-level
        results (see bcrabl_reports_without_components()). Returns
        {"deleted": [...], "failed": [...]} like clear_down_patient()."""
        return self._delete_resources(
            "DiagnosticReport", self.bcrabl_reports_without_components(reports))

    def bcrabl_reports_without_identifiers(self, reports):
        """Filters `reports` (e.g. from bcrabl_reports()) down to the ones
        with no `identifier` at all. Used by the Cepheid Test Results
        screen's "delete reports with no identifiers" action."""
        return [r for r in reports if not r.get("identifier")]

    def clear_down_bcrabl_reports_without_identifiers(self, reports):
        """
        DELETE every report in `reports` with no identifier at all (see
        bcrabl_reports_without_identifiers()), plus its associated
        Specimen — resolved the same way the Cepheid screen displays it
        (the report's own `specimen`, falling back to the originating
        order's if the report has none). A specimen is only deleted if
        none of the *other* reports in `reports` (the ones being kept)
        also reference it, so cleaning up a no-identifier report can't
        break a specimen a real report still relies on.

        Returns {"deleted": [...], "failed": [...]} like
        clear_down_patient().
        """
        targets = self.bcrabl_reports_without_identifiers(reports)
        target_ids = {r["id"] for r in targets if r.get("id")}

        def specimens_for(report):
            order = self.order_for_report(report)
            specimens = self.resolve_specimens(report)
            if not specimens and order:
                specimens = self.resolve_specimens(order)
            return specimens

        kept_specimen_ids = set()
        for r in reports:
            if r.get("id") in target_ids:
                continue
            kept_specimen_ids.update(s["id"] for s in specimens_for(r) if s.get("id"))

        specimens_to_delete = {}
        for r in targets:
            for s in specimens_for(r):
                if s.get("id") and s["id"] not in kept_specimen_ids:
                    specimens_to_delete[s["id"]] = s

        result = self._delete_resources("DiagnosticReport", targets)
        specimen_result = self._delete_resources("Specimen", list(specimens_to_delete.values()))
        result["deleted"].extend(specimen_result["deleted"])
        result["failed"].extend(specimen_result["failed"])
        return result

    @staticmethod
    def _identifier_keys(resource):
        """(system, value) tuples for every identifier on a resource —
        used as a grouping key for duplicate detection (see
        duplicate_bcrabl_reports()). Entries with no value are skipped
        (nothing to match on)."""
        return {
            (ident.get("system"), ident.get("value"))
            for ident in resource.get("identifier", []) if ident.get("value")
        }

    def duplicate_bcrabl_reports(self, reports):
        """
        Groups `reports` (e.g. from bcrabl_reports()) into clusters that
        share at least one identical identifier (system+value pair) — via
        union-find, so reports connected transitively through different
        shared identifiers still end up in one cluster. Reports with no
        identifiers at all never cluster with anything here (see
        bcrabl_reports_without_identifiers() for those — deliberately a
        separate action, since "no identifiers" isn't the same claim as
        "duplicate of a specific other report").

        Within each cluster of 2+ reports, the most-recently-updated one
        (by `meta.lastUpdated`, falling back to `issued`/
        `effectiveDateTime`) is kept; every other report in that cluster
        is returned as a duplicate to delete. Used by the Cepheid Test
        Results screen's "delete duplicate reports" action.
        """
        reports_by_id = {r["id"]: r for r in reports if r.get("id")}
        parent = {rid: rid for rid in reports_by_id}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        key_to_first_id = {}
        for rid, r in reports_by_id.items():
            for key in self._identifier_keys(r):
                if key in key_to_first_id:
                    union(rid, key_to_first_id[key])
                else:
                    key_to_first_id[key] = rid

        clusters = {}
        for rid in reports_by_id:
            clusters.setdefault(find(rid), []).append(reports_by_id[rid])

        def sort_key(r):
            return (r.get("meta") or {}).get("lastUpdated") or r.get("issued") or r.get("effectiveDateTime") or ""

        duplicates = []
        for cluster in clusters.values():
            if len(cluster) < 2:
                continue
            cluster.sort(key=sort_key, reverse=True)  # latest first
            duplicates.extend(cluster[1:])  # everything except the latest
        return duplicates

    def clear_down_duplicate_bcrabl_reports(self, reports):
        """DELETE every duplicate report found by
        duplicate_bcrabl_reports() (the latest in each identifier-sharing
        cluster is kept). Returns {"deleted": [...], "failed": [...]}
        like clear_down_patient()."""
        return self._delete_resources("DiagnosticReport", self.duplicate_bcrabl_reports(reports))

    # ---- Requester resolution -----------------------------------------

    def resolve_reference(self, ref):
        """Fetch whatever resource a Reference object points to, or None.
        Cached for the lifetime of the process, since stats aggregation
        resolves the same organisations/practitioners over and over."""
        ref_id = ref.get("reference", "") if ref else ""
        if not ref_id:
            return None
        if ref_id in self._ref_cache:
            return self._ref_cache[ref_id]
        try:
            resource = self._get(ref_id)
        except requests.HTTPError:
            resource = None
        self._ref_cache[ref_id] = resource
        return resource

    @staticmethod
    def _practitioner_name(practitioner):
        names = practitioner.get("name", [])
        if not names:
            return None
        n = names[0]
        given = " ".join(n.get("given", []))
        family = n.get("family", "")
        prefix = " ".join(n.get("prefix", []))
        full = " ".join(p for p in [prefix, given, family] if p)
        return full or None

    @classmethod
    def _practitioner_registration_code(cls, practitioner):
        """A requesting clinician's GMC or GMP registration number,
        formatted as "GMC 1234567" / "GMP 1234567", or None if the
        Practitioner resource carries neither identifier. GMC is checked
        first (see GMC_NUMBER_SYSTEM/GMP_NUMBER_SYSTEM above)."""
        gmc = cls._identifier_value(practitioner, cls.GMC_NUMBER_SYSTEM)
        if gmc:
            return f"GMC {gmc}"
        gmp = cls._identifier_value(practitioner, cls.GMP_NUMBER_SYSTEM)
        if gmp:
            return f"GMP {gmp}"
        return None

    @classmethod
    def _practitioner_display_name(cls, practitioner):
        """A practitioner's name with their GMC/GMP registration number
        (if any) appended in brackets — the shared formatting
        requester_display() and requesting_clinician_display() both use
        once they've resolved a Practitioner resource, whether reached
        directly or via a PractitionerRole. Returns None if the
        Practitioner resource has no name."""
        name = cls._practitioner_name(practitioner)
        if not name:
            return None
        registration_code = cls._practitioner_registration_code(practitioner)
        return f"{name} ({registration_code})" if registration_code else name

    @classmethod
    def _reference_identifier_code(cls, ref):
        """A logical reference's own inline `Reference.identifier`,
        formatted as "GMC 1234567" / "GMP 1234567" if its system matches
        one of the registration-number systems — the same GMC/GMP check
        _practitioner_registration_code() does against a resolved
        Practitioner resource's own identifier *list*, just read off a
        single inline `Identifier` on the Reference itself instead. Used
        when a PractitionerRole.practitioner is a logical reference
        (display + identifier, no resolvable `.reference`) rather than a
        normal reference to a fetchable Practitioner resource."""
        if not ref:
            return None
        identifier = ref.get("identifier") or {}
        value = identifier.get("value")
        if not value:
            return None
        system = identifier.get("system")
        if system == cls.GMC_NUMBER_SYSTEM:
            return f"GMC {value}"
        if system == cls.GMP_NUMBER_SYSTEM:
            return f"GMP {value}"
        return None

    def _resolve_practitioner_display(self, practitioner_ref):
        """Display name (+ GMC/GMP code) for a PractitionerRole.practitioner
        reference. Tries resolving it to a full Practitioner resource first
        (_practitioner_display_name()); when that fails — confirmed to
        happen on a real server (see requester_display()'s docstring): a
        PractitionerRole whose `.practitioner` is a *logical* reference
        (`display` + `identifier`, no `.reference` to actually fetch) —
        falls back to the reference's own inline `display`, with a GMC/GMP
        code from its inline `identifier` (_reference_identifier_code())
        appended the same way a resolved Practitioner's would be. Returns
        None if neither yields a name."""
        if not practitioner_ref:
            return None
        practitioner = self.resolve_reference(practitioner_ref)
        if practitioner:
            return self._practitioner_display_name(practitioner)
        display = practitioner_ref.get("display")
        if not display:
            return None
        registration_code = self._reference_identifier_code(practitioner_ref)
        return f"{display} ({registration_code})" if registration_code else display

    def requester_display(self, order):
        """
        Resolve ServiceRequest.requester (PractitionerRole | Practitioner |
        Organization — the IG's own examples use PractitionerRole, but FHIR
        R4 allows a direct Practitioner reference too, and at least one real
        server's data uses it) into a human-readable "Dr X (GMC 1234567)
        (Org Y)" style string — the GMC/GMP registration number (see
        _practitioner_registration_code()) is appended in brackets right
        after the clinician's name, before the organisation, only when the
        underlying Practitioner resource actually carries one.
        """
        requester_ref = order.get("requester")
        if not requester_ref:
            return "—"

        resource = self.resolve_reference(requester_ref)
        if resource is None:
            # Couldn't dereference it (auth, deleted, cross-server ref, etc).
            # Fall back to whatever display text was inlined on the reference.
            return requester_ref.get("display") or requester_ref.get("reference", "—")

        rtype = resource.get("resourceType")

        if rtype == "Organization":
            return resource.get("name") or requester_ref.get("display") or "—"

        if rtype == "Practitioner":
            return self._practitioner_display_name(resource) or requester_ref.get("display") or "—"

        if rtype == "PractitionerRole":
            practitioner_name = self._resolve_practitioner_display(resource.get("practitioner"))

            org_name = None
            org_ref = resource.get("organization")
            if org_ref:
                org = self.resolve_reference(org_ref)
                if org:
                    org_name = org.get("name")

            if practitioner_name and org_name:
                return f"{practitioner_name} ({org_name})"
            return practitioner_name or org_name or requester_ref.get("display") or "—"

        # Unexpected resource type — show what we can.
        return requester_ref.get("display") or resource.get("id", "—")

    def requesting_clinician_display(self, order):
        """
        The named individual clinician who requested this order — a
        Practitioner referenced directly by ServiceRequest.requester, or
        the Practitioner behind a requester PractitionerRole (see
        requester_display()'s docstring for why both shapes are handled),
        with their GMC/GMP registration number in brackets where present.
        A PractitionerRole.practitioner that's a *logical* reference
        (display + identifier, no resolvable Practitioner resource) still
        yields a name here too — see _resolve_practitioner_display().
        Returns "—" whenever no individual clinician is actually named:
        requester references an Organization directly, a PractitionerRole
        with no linked Practitioner at all, or a Practitioner/logical
        reference with no name/display. Distinct from requester_display()
        (used for the "Requested by"/"Requesting organisation"
        columns/fields elsewhere), which deliberately falls back to
        showing the requesting *organisation* name in those same cases —
        this method is specifically for a "requesting clinician" column,
        where an organisation name would be a wrong answer, not just a
        less specific one.
        """
        requester_ref = order.get("requester")
        if not requester_ref:
            return "—"

        resource = self.resolve_reference(requester_ref)
        if resource is None:
            return "—"

        rtype = resource.get("resourceType")

        if rtype == "Practitioner":
            return self._practitioner_display_name(resource) or "—"

        if rtype != "PractitionerRole":
            return "—"

        return self._resolve_practitioner_display(resource.get("practitioner")) or "—"

    def performer_display(self, order):
        """
        Display string for ServiceRequest.performer — the desired
        performer(s) for carrying out the requested test (e.g. a specific
        lab or reporting scientist), 0..* references to Practitioner |
        PractitionerRole | Organization | CareTeam | HealthcareService |
        Patient | Device | RelatedPerson per FHIR R4. Same resolution/
        format as results_interpreter_display() — "Name (Type)", with a
        PractitionerRole drilling down to the underlying Practitioner's own
        name where it resolves (same pattern as requester_display()) —
        joined with "; " for multiple performers. Returns "—" if performer
        is absent or empty.
        """
        entries = []
        for ref in order.get("performer", []):
            resource = self.resolve_reference(ref)
            if resource is None:
                name = ref.get("display") or ref.get("reference") or "Unknown"
                entries.append(f"{name} (Unknown)")
                continue

            rtype = resource.get("resourceType")
            if rtype == "Practitioner":
                name = self._practitioner_name(resource) or ref.get("display") or "Unknown"
            elif rtype == "PractitionerRole":
                practitioner_ref = resource.get("practitioner")
                practitioner = self.resolve_reference(practitioner_ref) if practitioner_ref else None
                name = (practitioner and self._practitioner_name(practitioner)) or ref.get("display") or "Unknown"
            elif rtype == "Organization":
                name = resource.get("name") or ref.get("display") or "Unknown"
            else:
                name = ref.get("display") or resource.get("id") or "Unknown"

            entries.append(f"{name} ({rtype or 'Unknown'})")
        return "; ".join(entries) if entries else "—"

    # ------------------------------------------------------------------
    # econcur import — NHS ODS "English Hospital Consultants" CSV export
    # https://digital.nhs.uk/services/organisation-data-service/data-search-and-export/csv-downloads/miscellaneous
    #
    # Imports it as Practitioner + PractitionerRole resources, matching on
    # GMC number so re-running this updates existing entries rather than
    # duplicating them. Driven by the admin screen at /admin/econcur-import
    # (see app.py) via import_econcur() below.
    # ------------------------------------------------------------------

    #: The econcur.csv download itself — a plain CSV export, not a FHIR
    #: endpoint, so fetch_econcur_csv() is a bare requests.get(), same as
    #: ods_lookup_name()'s ODS_LOOKUP_API_URL above. ~75,000 rows, no
    #: header row.
    ECONCUR_CSV_URL = "https://www.odsdatasearchandexport.nhs.uk/api/getReport?report=econcur"

    #: UK Core CodeSystem for NHS Data Dictionary "Main Specialty Code",
    #: used for PractitionerRole.specialty.coding.system below. The bare
    #: code (e.g. "300") is stored regardless, so a wrong system URI here
    #: wouldn't lose data, it would just mean a consumer can't resolve the
    #: coding as belonging to a recognised system.
    MAIN_SPECIALTY_CODE_SYSTEM = "https://fhir.hl7.org.uk/CodeSystem/UKCore-PracticeSettingCode"

    #: Composite identifier system for the PractitionerRole itself,
    #: uniquely identifying a (practitioner, organization) membership by
    #: GMC number + ODS code — import_econcur()'s own business key for
    #: this specific role, distinct from GMC_NUMBER_SYSTEM/
    #: ODS_ORGANIZATION_CODE_SYSTEM (which identify the Practitioner/
    #: Organization resources, not the role). A PractitionerRole carrying
    #: this identifier (plus an identifier on each of its `practitioner`/
    #: `organization` references — see _import_econcur_row()) is treated
    #: as "settled": import_econcur() never updates it again once all
    #: three are present (see _econcur_role_has_identifiers()).
    PRACTITIONER_ROLE_GMC_ODS_SYSTEM = "https://fhir.nwgenomics.nhs.uk/Identifier/PractitionerRole-GMC-ODS"

    #: econcur.csv column indexes (0-based), per the ODS Reference Data
    #: Catalogue's "Hospital Consultants" spec — 13 columns, no header:
    #:   0 GMC code (bare digits), 1 ODS practitioner code (same GMC code
    #:   prefixed "C"), 2 name ("SURNAME INITIAL(S)"), 3 initials (unused,
    #:   always blank), 4 sex (unused, always blank), 5 main specialty
    #:   code(s) (pipe-separated when a consultant holds more than one at
    #:   this location), 6 practitioner type (unused, always blank),
    #:   7 location organisation code (the employing trust's ODS code),
    #:   8-12 unused. Consultants with more than one active membership
    #:   appear as separate rows sharing the same GMC code, differing
    #:   only in the location organisation code.
    #:
    #:   parse_econcur_row() below reads column 0 but formats it through
    #:   _format_gmc_number() (-> "C" + digits) rather than using it bare
    #:   — this IG's gmc-number identifier *value* format is "C" + digits
    #:   (see _format_gmc_number()'s docstring), not the bare number ODS
    #:   itself calls the "GMC code". Column 1 already has this prefix in
    #:   the source file, but deriving it from column 0 instead keeps a
    #:   single source of truth for the format rather than trusting the
    #:   file to always agree with itself.
    ECONCUR_COL_GMC = 0
    ECONCUR_COL_NAME = 2
    ECONCUR_COL_SPECIALTY = 5
    ECONCUR_COL_ORG_CODE = 7

    @classmethod
    def fetch_econcur_csv(cls):
        """Downloads the raw econcur.csv text from ECONCUR_CSV_URL. This
        hits NHS Digital's public ODS export API directly, not the
        configured FHIR server — no auth needed, but it does need outbound
        internet from wherever the Flask app runs (same requirement as
        ods_lookup_name()/geocode_postcode()/fetch_icb_boundaries()
        elsewhere in this file). The file is a few MB, so the timeout is
        generous accordingly."""
        resp = requests.get(cls.ECONCUR_CSV_URL, timeout=120)
        resp.raise_for_status()
        resp.encoding = resp.encoding or "utf-8"
        return resp.text

    @classmethod
    def parse_econcur_row(cls, row):
        """Parses one econcur.csv row (see the column map above) into
        {"gmc", "name", "specialties", "org_code"}, or None if it's
        missing a field this import treats as mandatory (GMC code, name,
        or location organisation code — the three fields a usable
        Practitioner + PractitionerRole pair needs). `gmc` is always
        "C"-prefixed (_format_gmc_number()) — this IG's required
        gmc-number identifier value format — not the bare digits column 0
        holds."""
        if len(row) <= cls.ECONCUR_COL_ORG_CODE:
            return None
        gmc = cls._format_gmc_number(row[cls.ECONCUR_COL_GMC].strip())
        name = row[cls.ECONCUR_COL_NAME].strip()
        org_code = row[cls.ECONCUR_COL_ORG_CODE].strip()
        if not gmc or not name or not org_code:
            return None
        specialty_raw = row[cls.ECONCUR_COL_SPECIALTY].strip()
        specialties = [s for s in specialty_raw.split("|") if s] if specialty_raw else []
        return {"gmc": gmc, "name": name, "specialties": specialties, "org_code": org_code}

    @staticmethod
    def _split_econcur_name(name):
        """Best-effort family/given split of econcur's single
        "SURNAME INITIAL(S)" field (e.g. "BLACKETT K") — the last
        whitespace-separated token is treated as the given name/initials,
        everything before it as the family name. Not validated against
        anything beyond the sample rows seen in a real download; a
        multi-word surname (e.g. "VAN DYK J") would misparse under this
        heuristic. The original string is always kept as `.text` too, so
        display doesn't depend on the split being right."""
        parts = name.split()
        if len(parts) < 2:
            return name, []
        return " ".join(parts[:-1]), [parts[-1]]

    def all_organizations_by_ods(self, max_pages=100):
        """Every Organization on this server with an ODS-code identifier,
        keyed by that code. Still one unfiltered system-wide search —
        unlike Practitioner/PractitionerRole/Patient below, Organization
        hasn't (yet) been seen to 413 on a real server, presumably
        because there are far fewer organisations than practitioners or
        patients on this CDR. If this one starts 413ing too, it needs
        the same organisation-batching treatment as the others — though
        that's circular for Organization itself, so it would need a
        different partition key (ODS region code, alphabetical range,
        etc.)."""
        orgs = self._search_all("Organization", {"_count": 100}, max_pages=max_pages)
        by_ods = {}
        for o in orgs:
            ods = self.organisation_ods_code(o)
            if ods:
                by_ods[ods] = o
        return by_ods

    def _find_practitioner_by_gmc(self, gmc):
        """Single, targeted Practitioner lookup by GMC-number identifier
        — matches at most one resource, so unlike an unfiltered
        `Practitioner` search this stays well inside whatever makes this
        CDR (HealthConnect, per FHIR_BASE_URL) reject a genuinely
        unscoped query (see "413s on unfiltered system-wide searches" in
        CLAUDE.md — confirmed on a real server that neither a smaller
        `_count` nor an unrelated dummy filter parameter avoids this;
        the fix that actually worked was scoping the *search itself* to
        something narrow, per the CDR's suggested fix of batching by
        organisation — this is the same idea applied per-identifier
        instead, for the one case org-batching alone can't cover, see
        _import_econcur_row()). `gmc` is expected "C"-prefixed
        (parse_econcur_row() always produces it that way now); if that
        exact value doesn't match anything, also tries the bare digits —
        a Practitioner created by an import run from before
        _format_gmc_number() existed may still have the un-prefixed
        value stored, and this avoids treating it as unseen and creating
        a duplicate. Returns the first match, or None."""
        bundle = self._get("Practitioner", {"identifier": f"{self.GMC_NUMBER_SYSTEM}|{gmc}", "_count": 1})
        entries = self._entries(bundle)
        if entries:
            return entries[0]
        bare_gmc = gmc.lstrip("Cc").strip() if gmc else None
        if bare_gmc and bare_gmc != gmc:
            bundle = self._get("Practitioner", {"identifier": f"{self.GMC_NUMBER_SYSTEM}|{bare_gmc}", "_count": 1})
            entries = self._entries(bundle)
        return entries[0] if entries else None

    def _search_all_by_organization(self, resource_type, organizations, extra_params=None, count=50, max_pages_per_org=20):
        """Fetches `resource_type` resources scoped to each Organization
        in `organizations` — one org at a time, via the standard
        `organization` search param (Patient.managingOrganization /
        PractitionerRole.organization) — instead of one unfiltered
        system-wide search. This CDR 413s on the latter regardless of
        `_count` or an unrelated extra parameter (see "413s on
        unfiltered system-wide searches" in CLAUDE.md); scoping each
        individual query to one real organisation is what actually
        avoids it, matching the fix suggested against the real server.
        `organizations` is any iterable of Organization resources with
        an `id` (e.g. `all_organizations_by_ods().values()`); one
        without an `id` is skipped. Returns (matches, included) pooled
        across every organisation's own paginated search — a resource
        this server has attributed to more than one organisation isn't
        expected, but would just appear once per org here (harmless:
        every caller of this method keys its own result by the
        resource's own id/identifier, which naturally de-dupes)."""
        all_matches, all_included = [], []
        for organization in organizations:
            org_id = organization.get("id")
            if not org_id:
                continue
            params = {"organization": f"Organization/{org_id}", "_count": count}
            if extra_params:
                params.update(extra_params)
            matches, included = self._search_all_split(resource_type, params, max_pages=max_pages_per_org)
            all_matches.extend(matches)
            all_included.extend(included)
        return all_matches, all_included

    @classmethod
    def _econcur_role_has_identifiers(cls, role):
        """Whether an existing PractitionerRole already carries all three
        identifiers import_econcur() writes on create — the composite
        GMC+ODS identifier (PRACTITIONER_ROLE_GMC_ODS_SYSTEM) on the role
        itself, plus an `identifier` on each of its `practitioner`/
        `organization` references. Once all three are present, the role
        is treated as "settled" and import_econcur() never updates it
        again (see _import_econcur_row()) — a role missing one or more
        (i.e. created before this identifier scheme existed) gets them
        backfilled instead, one time, which is what makes it settled
        from then on."""
        has_role_identifier = any(
            ident.get("system") == cls.PRACTITIONER_ROLE_GMC_ODS_SYSTEM
            for ident in role.get("identifier", [])
        )
        has_practitioner_identifier = bool((role.get("practitioner") or {}).get("identifier"))
        has_organization_identifier = bool((role.get("organization") or {}).get("identifier"))
        return has_role_identifier and has_practitioner_identifier and has_organization_identifier

    def _import_econcur_row(self, parsed, apply, practitioners_by_gmc, organizations_by_ods, roles_by_key, result):
        """One econcur.csv row's worth of create-or-match work, shared by
        import_econcur()'s per-row loop. Mutates the three preloaded dicts
        in place so a later row referencing the same GMC number or ODS
        code (very common — see the column map above) reuses what this
        row just created instead of creating a second copy, whether or
        not `apply` is actually writing anything (see the "pending-"
        placeholder ids below for the dry-run case).

        Practitioner and Organization are create-only: once a GMC number
        or ODS code has a matching resource, it's left alone forever,
        even if econcur.csv's name for it has since changed — see
        import_econcur()'s docstring for why."""
        gmc = parsed["gmc"]
        family, given = self._split_econcur_name(parsed["name"])

        practitioner = practitioners_by_gmc.get(gmc)
        if practitioner is None:
            # Not seen via the organisation-batched preload — could
            # genuinely be new, or could already exist with a role only
            # at an organisation not yet reached in this run (or with no
            # role at all yet). A single targeted identifier search is
            # cheap enough for this CDR even though an unfiltered
            # Practitioner dump isn't (see _find_practitioner_by_gmc()),
            # so it's worth checking before concluding "create new".
            practitioner = self._find_practitioner_by_gmc(gmc)
        if practitioner is None:
            new_practitioner = {
                "resourceType": "Practitioner",
                "identifier": [{"system": self.GMC_NUMBER_SYSTEM, "value": gmc}],
                "name": [{"text": parsed["name"], "family": family, "given": given}],
            }
            if apply:
                practitioner = self._post("Practitioner", new_practitioner)
                if not practitioner or not practitioner.get("id"):
                    raise requests.RequestException(f"Practitioner create for GMC {gmc} returned no id")
            else:
                # No real id yet in a dry run — a placeholder lets later
                # rows for the same GMC number still dedupe against this
                # one within the same preview, matching what apply=True
                # would actually do.
                practitioner = {**new_practitioner, "id": f"pending-{gmc}"}
            practitioners_by_gmc[gmc] = practitioner
            result["practitioners_created"] += 1
        else:
            # Cache it (whether it came from the preload dict already, or
            # from the fallback identifier search just above) so a later
            # row for the same GMC number — very common, see the column
            # map above — doesn't repeat that search.
            practitioners_by_gmc[gmc] = practitioner
            result["practitioners_matched"] += 1

        org_code = parsed["org_code"]
        organization = organizations_by_ods.get(org_code)
        if organization is None:
            # Per the "create a stub Organization" choice for this import:
            # a location organisation code with no matching Organization
            # gets a minimal one (ODS code only, no name) rather than the
            # row being skipped — fix_organization_names.py can backfill
            # the name from the same ODS code later.
            new_org = {
                "resourceType": "Organization",
                "identifier": [{"system": self.ODS_ORGANIZATION_CODE_SYSTEM, "value": org_code}],
            }
            if apply:
                organization = self._post("Organization", new_org)
                if not organization or not organization.get("id"):
                    raise requests.RequestException(f"Organization create for ODS {org_code} returned no id")
            else:
                organization = {**new_org, "id": f"pending-org-{org_code}"}
            organizations_by_ods[org_code] = organization
            result["organizations_created"] += 1
        else:
            result["organizations_matched"] += 1

        practitioner_ref = f"Practitioner/{practitioner['id']}"
        organization_ref = f"Organization/{organization['id']}"
        role_key = (practitioner_ref, organization_ref)
        specialty = [
            {"coding": [{"system": self.MAIN_SPECIALTY_CODE_SYSTEM, "code": code}]}
            for code in parsed["specialties"]
        ]
        # Both references carry their target's own business identifier
        # alongside the internal .reference, and the role itself carries
        # a composite GMC+ODS identifier — see PRACTITIONER_ROLE_GMC_ODS_SYSTEM
        # and _econcur_role_has_identifiers() for what these are for.
        practitioner_ref_obj = {
            "reference": practitioner_ref,
            "identifier": {"system": self.GMC_NUMBER_SYSTEM, "value": gmc},
        }
        organization_ref_obj = {
            "reference": organization_ref,
            "identifier": {"system": self.ODS_ORGANIZATION_CODE_SYSTEM, "value": org_code},
        }
        role_identifier = {"system": self.PRACTITIONER_ROLE_GMC_ODS_SYSTEM, "value": f"{gmc}-{org_code}"}

        role = roles_by_key.get(role_key)
        if role is None:
            new_role = {
                "resourceType": "PractitionerRole",
                "identifier": [role_identifier],
                "practitioner": practitioner_ref_obj,
                "organization": organization_ref_obj,
                "specialty": specialty,
            }
            if apply:
                role = self._post("PractitionerRole", new_role)
                if not role or not role.get("id"):
                    raise requests.RequestException(
                        f"PractitionerRole create for GMC {gmc}/org {org_code} returned no id"
                    )
            else:
                role = {**new_role, "id": f"pending-role-{gmc}-{org_code}"}
            roles_by_key[role_key] = role
            result["roles_created"] += 1
        elif self._econcur_role_has_identifiers(role):
            # Settled — carries all three identifiers already, so never
            # updated again, even if its specialty no longer matches
            # econcur.csv (same "create-only once identified" reasoning
            # as Practitioner/Organization above).
            result["roles_unchanged"] += 1
        else:
            # A legacy role predating this identifier scheme — backfilled
            # once (identifiers plus a specialty refresh while already
            # writing), which is what makes it settled for next time.
            if apply:
                updated_role = dict(role)
                updated_role["identifier"] = [role_identifier]
                updated_role["practitioner"] = practitioner_ref_obj
                updated_role["organization"] = organization_ref_obj
                updated_role["specialty"] = specialty
                self._put(f"PractitionerRole/{role['id']}", updated_role)
                roles_by_key[role_key] = updated_role
            result["roles_updated"] += 1

    def import_econcur(self, csv_text, apply=False, progress=None):
        """
        Imports an econcur.csv export (see fetch_econcur_csv()) as
        Practitioner + PractitionerRole resources on this FHIR server.

        Matching, so re-running this doesn't duplicate — but Practitioner
        and Organization are **create-only**: once one exists, it's never
        rewritten, even if econcur.csv's data for it has since changed.
          - Practitioner, by GMC-number identifier (GMC_NUMBER_SYSTEM) —
            an unseen GMC number gets a new Practitioner; an existing one
            is left alone (`practitioners_matched`).
          - Organization (the row's location organisation code, an ODS
            trust code), by ODS-code identifier
            (ODS_ORGANIZATION_CODE_SYSTEM) — an unmatched code gets a
            minimal stub Organization created (see _import_econcur_row);
            an existing one is left alone (`organizations_matched`).
            `fix_organization_names.py` is still how a stub's name gets
            backfilled, not this import.
          - PractitionerRole, by the (practitioner, organization) pair —
            one consultant can hold more than one active membership
            (separate rows, same GMC, different org code). A new role
            gets created with three identifiers: a composite GMC+ODS
            identifier on the role itself
            (PRACTITIONER_ROLE_GMC_ODS_SYSTEM), plus the target
            Practitioner's GMC number / Organization's ODS code as an
            `identifier` on the role's own `practitioner`/`organization`
            references (not just a bare `.reference`). **A role that
            already carries all three is "settled" and never updated
            again** (`roles_unchanged` — see
            `_econcur_role_has_identifiers()`), same create-only
            reasoning as Practitioner/Organization. A role from before
            this identifier scheme existed (missing one or more) gets
            them backfilled once, refreshing `specialty` to match
            econcur.csv at the same time since it's already being
            written (`roles_updated`) — after that one backfill it's
            settled too.

        apply=False (the default) runs the full matching logic and
        returns exactly the counts apply=True would produce, without
        calling _post()/_put() — same dry-run convention as
        scripts/fix_organization_names.py's --apply flag.

        `progress`, if given, is called as progress(processed, total)
        every 500 rows (and once at the end) — a row count for a caller
        to surface during what can be a long run (the full export is
        tens of thousands of rows, one to several HTTP round trips each
        when apply=True). A row that raises requests.RequestException (a
        create/update that failed) is recorded in result["errors"] and
        the import continues rather than aborting.

        The Practitioner/PractitionerRole matching dicts are preloaded
        by *organisation* (`_search_all_by_organization()`, one
        `PractitionerRole?organization=...&_include=...:practitioner`
        search per Organization on the server) rather than one
        unfiltered system-wide search — a real HealthConnect CDR (this
        app's FHIR_BASE_URL) 413s on the latter regardless of `_count`
        or an unrelated filter parameter (see "413s on unfiltered
        system-wide searches" in CLAUDE.md), and organisation-scoped
        batching was the fix confirmed to actually work against it. This
        can't fully replace a global-by-GMC lookup on its own, though: a
        Practitioner who already exists but only has a role at an
        organisation this preload hasn't reached yet wouldn't be found
        by it — _import_econcur_row() covers that gap with a single
        targeted per-GMC search (_find_practitioner_by_gmc()) whenever
        the org-batched preload comes up empty, rather than risking a
        duplicate Practitioner.
        """
        rows = list(csv.reader(io.StringIO(csv_text)))
        total = len(rows)

        organizations_by_ods = self.all_organizations_by_ods()

        practitioners_by_gmc = {}
        roles_by_key = {}
        role_matches, role_included = self._search_all_by_organization(
            "PractitionerRole", organizations_by_ods.values(),
            extra_params={"_include": "PractitionerRole:practitioner"},
        )
        practitioners_by_id = {
            r["id"]: r for r in role_included
            if r.get("resourceType") == "Practitioner" and r.get("id")
        }
        for role in role_matches:
            practitioner_ref = (role.get("practitioner") or {}).get("reference")
            organization_ref = (role.get("organization") or {}).get("reference")
            if practitioner_ref and organization_ref:
                roles_by_key[(practitioner_ref, organization_ref)] = role
            if practitioner_ref and practitioner_ref.startswith("Practitioner/"):
                practitioner = practitioners_by_id.get(practitioner_ref.split("/", 1)[1])
                if practitioner:
                    # Normalized through _format_gmc_number() so a
                    # Practitioner whose stored identifier still has the
                    # pre-fix bare-digit value (created before
                    # parse_econcur_row() started "C"-prefixing) is keyed
                    # the same way an incoming CSV row's gmc is, and gets
                    # matched instead of duplicated.
                    gmc = self._format_gmc_number(self._identifier_value(practitioner, self.GMC_NUMBER_SYSTEM))
                    if gmc:
                        practitioners_by_gmc[gmc] = practitioner

        result = {
            "total_rows": total,
            "invalid_rows": 0,
            "practitioners_created": 0, "practitioners_matched": 0,
            "organizations_created": 0, "organizations_matched": 0,
            "roles_created": 0, "roles_updated": 0, "roles_unchanged": 0,
            "errors": [],
        }

        for i, raw_row in enumerate(rows):
            if progress and i % 500 == 0:
                progress(i, total)
            parsed = self.parse_econcur_row(raw_row)
            if parsed is None:
                result["invalid_rows"] += 1
                continue
            try:
                self._import_econcur_row(
                    parsed, apply, practitioners_by_gmc, organizations_by_ods, roles_by_key, result,
                )
            except requests.RequestException as e:
                result["errors"].append(f"GMC {parsed['gmc']} / org {parsed['org_code']}: {e}")

        if progress:
            progress(total, total)
        return result

    # ------------------------------------------------------------------
    # Order creation — /order/new (app.py)
    #
    # Builds a genomic test order as a downloadable FHIR message Bundle,
    # laid out after NW GLH's "Genomic Testing Request Form (Rare
    # Disease)" (DOC4900 —
    # https://mft.nhs.uk/nwglh/documents/test-request-forms/), this IG's
    # own ServiceRequest/Specimen profiles, its GenomicTestOrder
    # Questionnaire's "Ask At Order Entry" section, and the worked
    # message-Bundle example (all at https://nw-gmsa.github.io/).
    # Patient, requesting organisation, and requesting clinician are
    # always resources already on this FHIR server — searched and
    # picked, never freely typed — see search_organizations()/
    # practitioners_for_organization() below and search_patients()
    # above. NOTHING here is written back to the FHIR server — see
    # build_order_message_bundle()'s docstring.
    # ------------------------------------------------------------------

    def search_organizations(self, name=None, ods_code=None):
        """Organization search for the order-create screen's "requesting
        organisation" picker — filtered by name or ODS code, never
        unfiltered (an unfiltered system-wide Organization search 413s
        on this CDR — see all_organizations_by_ods()/CLAUDE.md's "413s
        on unfiltered system-wide searches"). Returns [] without
        querying at all if neither is given, rather than risking an
        accidentally-unfiltered search."""
        if ods_code:
            bundle = self._get("Organization", {"identifier": ods_code, "_count": 20})
            return self._entries(bundle)
        if name:
            bundle = self._get("Organization", {"name": name, "_count": 20})
            return self._entries(bundle)
        return []

    def practitioners_for_organization(self, organization_id, count=100):
        """Practitioners associated with `organization_id` via an
        existing PractitionerRole — PractitionerRole.organization is
        this IG's actual clinician-to-organisation linkage (the same one
        import_econcur() populates for hospital consultants), so the
        order-create screen's "requesting clinician" picker is scoped to
        it instead of an open name search across every Practitioner on
        the server. `_include=PractitionerRole:practitioner` pulls the
        Practitioner resources back in the same query; if a server
        doesn't tag `search.mode` reliably on `_include`d entries (a
        real quirk this app has hit before — see ctdna_orders() in
        CLAUDE.md) and none come back that way, falls back to resolving
        each role's practitioner reference directly."""
        matches, included = self._search_all_split("PractitionerRole", {
            "organization": f"Organization/{organization_id}",
            "_include": "PractitionerRole:practitioner",
            "_count": count,
        })
        practitioners = {
            r["id"]: r for r in included
            if r.get("resourceType") == "Practitioner" and r.get("id")
        }
        if not practitioners and matches:
            for role in matches:
                practitioner = self.resolve_reference(role.get("practitioner") or {})
                if practitioner and practitioner.get("id"):
                    practitioners[practitioner["id"]] = practitioner
        return sorted(practitioners.values(), key=lambda p: self._practitioner_name(p) or "")

    #: Local identifier system for placer order numbers minted by this
    #: app's own order-create screen — there is no external
    #: order-numbering system integrated, so this app issues its own
    #: under the same "https://fhir.nwgenomics.nhs.uk/..." local-system
    #: convention as IGENE_PATIENT_IDENTIFIER_SYSTEM/
    #: SPECIMEN_IDENTIFIER_SYSTEM above.
    ORDER_PLACER_NUMBER_SYSTEM = "https://fhir.nwgenomics.nhs.uk/Id/lab-explorer-order-number"

    @classmethod
    def generate_order_placer_number(cls):
        """A short, human-typeable placer order number — "LE" (Lab
        Explorer) + today's date + a random 6-hex-digit suffix, e.g.
        "LE20260807-A1B2C3". Collisions are astronomically unlikely
        (16.7M possible suffixes per day) and not checked for."""
        return f"LE{date.today():%Y%m%d}-{secrets.token_hex(3).upper()}"

    #: The IG's own published GenomicTestCode CodeSystem — a ~2,100-entry
    #: fragment of England's National Genomic Test Directory (same
    #: underlying codes as GENOMIC_TEST_DIRECTORY_SYSTEM above, this is
    #: just where the IG publishes the actual code/display list rather
    #: than only the system URI). Powers the order-create screen's R-code
    #: <select> — every option offered there is a real code from this
    #: CodeSystem, not free text.
    GENOMIC_TEST_DIRECTORY_CODESYSTEM_URL = "https://nw-gmsa.github.io/en/CodeSystem-GenomicTestCode.json"

    #: Class-level cache, same reasoning as _icb_boundary_cache above —
    #: this is static reference data, not tied to any one FhirClient
    #: instance/server.
    _genomic_test_directory_cache = None

    @classmethod
    def genomic_test_directory_codes(cls):
        """Every {"code", "display"} pair from GENOMIC_TEST_DIRECTORY_CODESYSTEM_URL,
        sorted by code. Cached at class level for the process lifetime
        (same pattern as fetch_icb_boundaries() — a similarly-sized
        static reference dataset fetched from an external host); a
        failed fetch is *not* cached, so the next call retries. Returns
        [] if the fetch fails or the response has no `concept` array —
        callers should degrade to "code list unavailable" rather than
        raising, same as fetch_icb_boundaries()'s callers degrade to "no
        map"."""
        if cls._genomic_test_directory_cache is not None:
            return cls._genomic_test_directory_cache
        try:
            resp = requests.get(cls.GENOMIC_TEST_DIRECTORY_CODESYSTEM_URL, timeout=30)
            if resp.ok:
                concepts = resp.json().get("concept", [])
                codes = sorted(
                    (
                        {"code": c["code"], "display": c.get("display") or c["code"]}
                        for c in concepts if c.get("code")
                    ),
                    key=lambda c: c["code"],
                )
                if codes:
                    cls._genomic_test_directory_cache = codes
        except requests.RequestException:
            pass
        return cls._genomic_test_directory_cache or []

    @classmethod
    def genomic_test_directory_display(cls, code):
        """Display text for `code` from genomic_test_directory_codes(),
        or the bare code itself if the lookup fails or doesn't contain
        it (e.g. the cached fetch errored) — used by
        build_order_message_bundle() so ServiceRequest.code's display/
        `.text` always matches what the R-code select actually offered,
        rather than trusting a second free-typed field."""
        for entry in cls.genomic_test_directory_codes():
            if entry["code"] == code:
                return entry["display"]
        return code

    #: This IG's own CodeSystem for clinical indications — confirmed by
    #: the worked example's own ServiceRequest.reasonCode
    #: (https://nw-gmsa.github.io/en/Bundle-GenomicsOrderMessageCodedEntries.html,
    #: `{"system": "https://fhir.nwgenomics.nhs.uk/CodeSystem/GenomicClinicalIndication",
    #: "code": "R240", ...}` for test code "R240.1" — i.e. the indication
    #: code is the test code's prefix before the "."). Used for
    #: ServiceRequest.reasonCode in build_order_message_bundle() below,
    #: derived automatically from whichever test_code was picked rather
    #: than needing its own form field.
    GENOMIC_CLINICAL_INDICATION_SYSTEM = "https://fhir.nwgenomics.nhs.uk/CodeSystem/GenomicClinicalIndication"

    @classmethod
    def genomic_clinical_indications(cls):
        """Clinical indications derived from genomic_test_directory_codes()
        — the R/M code's own two-part structure encodes this: the part
        before the "." is the indication code (e.g. "M1" from "M1.1"),
        and the part of the test's display text before its first comma
        is the indication's description (e.g. "Colorectal Carcinoma"
        from "M1.1"'s "Colorectal Carcinoma, Multi-target NGS panel,
        small variant (KRAS, NRAS, BRAF)" — a display with no comma at
        all just uses the whole thing). One entry per distinct indication
        code, using the first matching test code's description (codes
        sharing an indication prefix share the same leading phrase).
        Powers the order-create screen's "Clinical indication" <select>,
        which narrows the R/M code <select> to just that indication's
        codes client-side (see order_new.html) — this method doesn't
        need its own form field/round trip for that."""
        indications = {}
        for entry in cls.genomic_test_directory_codes():
            indication_code = entry["code"].split(".")[0]
            if indication_code not in indications:
                indications[indication_code] = entry["display"].split(",")[0].strip()
        return [{"code": code, "display": display} for code, display in indications.items()]

    @classmethod
    def genomic_clinical_indication_display(cls, indication_code):
        """Description for an indication_code from
        genomic_clinical_indications(), or the bare code itself if it's
        somehow not in the list — same trust-the-source-list pattern as
        genomic_test_directory_display()."""
        for entry in cls.genomic_clinical_indications():
            if entry["code"] == indication_code:
                return entry["display"]
        return indication_code

    #: "Ask At Order Entry Questions" — the linkId "AskAtOrderEntry"
    #: group from the IG's GenomicTestOrder Questionnaire
    #: (https://nw-gmsa.github.io/en/Questionnaire-GenomicTestOrder.html,
    #: canonical URL https://fhir.nwgenomics.nhs.uk/Questionnaire/GenomicTestOrder,
    #: version 2.1.6). Hardcoded rather than fetched live — unlike the
    #: 2,100-entry test-code CodeSystem above, this is a small, stable
    #: set of 7 questions, and reproducing the Questionnaire's generic
    #: nested-item/enableWhen structure just to render these specific
    #: fields would be a lot of machinery for one fixed section.
    #:
    #: Each answered question becomes its own Observation, referenced
    #: from ServiceRequest.supportingInfo — not a QuestionnaireResponse —
    #: matching the Observation-per-question shape the IG's own worked
    #: example uses for these exact questions (OBX-Consanguinity,
    #: OBX-Pregnancy, OBX-PregnancyExpectedDeliveryDate, ... at
    #: https://nw-gmsa.github.io/en/Bundle-GenomicsOrderMessageCodedEntries.html).
    #: `value_type` says which Observation.value[x] to build (see
    #: _build_aoe_observation()) — taken from each Questionnaire item's
    #: own `definition` element, not guessed.
    #:
    #: The nested "pregnant" sub-group (only relevant when
    #: "Neonatal/Prenatal/Neither?" = Pregnancy) is flattened into this
    #: one list rather than reproducing the Questionnaire's `enableWhen`
    #: logic in Python — `shown_when` records the same condition so
    #: order_new.html can show/hide those three fields with a small bit
    #: of inline JS instead.
    ASK_AT_ORDER_ENTRY_QUESTIONS = [
        {
            "link_id": "SNM/842009",
            "text": "Patient is from consanguineous union?",
            "value_type": "codeable_concept",
            "code": {"system": "http://snomed.info/sct", "code": "842009", "display": "Consanguinity"},
            "options": [
                {"system": "http://loinc.org", "code": "LA33-6", "display": "Yes"},
                {"system": "http://loinc.org", "code": "LA32-8", "display": "No"},
                {"system": "http://loinc.org", "code": "LA4489-6", "display": "Unknown"},
            ],
        },
        {
            "link_id": "SNM/74996004-pathology-report",
            "text": "Confirm that a pathology report will be provided alongside the sample.",
            "value_type": "codeable_concept",
            "code": {"system": "http://snomed.info/sct", "code": "74996004", "display": "Confirmation of"},
            "options": [
                {"system": "http://loinc.org", "code": "LA33-6", "display": "Yes"},
                {"system": "http://loinc.org", "code": "LA32-8", "display": "No"},
                {"system": "http://loinc.org", "code": "LA4489-6", "display": "Unknown"},
            ],
        },
        {
            "link_id": "SNM/118185001",
            "text": "Neonatal/Prenatal/Neither?",
            "value_type": "codeable_concept",
            "code": {"system": "http://snomed.info/sct", "code": "118185001", "display": "Finding related to pregnancy"},
            "options": [
                {"system": "http://snomed.info/sct", "code": "77386006", "display": "Pregnancy"},
                {"system": "http://snomed.info/sct", "code": "255407002", "display": "Neonatal"},
                {"system": "http://loinc.org", "code": "LA32-8", "display": "No"},
            ],
        },
        {
            "link_id": "SNM/370386005",
            "text": "Does this test relate to a pregnancy with more than 1 fetus?",
            "value_type": "codeable_concept",
            "code": {"system": "http://snomed.info/sct", "code": "370386005", "display": "Ultrasound scan - multiple fetus"},
            "options": [
                {"system": "http://loinc.org", "code": "LA33-6", "display": "Yes"},
                {"system": "http://loinc.org", "code": "LA32-8", "display": "No"},
                {"system": "http://loinc.org", "code": "LA4489-6", "display": "Unknown"},
            ],
            "shown_when": {"link_id": "SNM/118185001", "code": "77386006"},
        },
        {
            "link_id": "SNM/161714006",
            "text": "Patient expected delivery date",
            "value_type": "date_time",
            "code": {"system": "http://snomed.info/sct", "code": "161714006", "display": "Estimated date of delivery"},
            "shown_when": {"link_id": "SNM/118185001", "code": "77386006"},
        },
        {
            "link_id": "SNM/598151000005105",
            "text": "Patient gestation",
            "value_type": "quantity",
            "code": {"system": "http://snomed.info/sct", "code": "57036006", "display": "Fetal gestational age"},
            "unit": {"unit": "wk", "system": "http://unitsofmeasure.org", "code": "wk"},
            "shown_when": {"link_id": "SNM/118185001", "code": "77386006"},
        },
        {
            "link_id": "SNM/17369002",
            "text": "Is this test for a pregnancy loss?",
            "value_type": "codeable_concept",
            "code": {"system": "http://snomed.info/sct", "code": "17369002", "display": "Miscarriage"},
            "options": [
                {"system": "http://loinc.org", "code": "LA33-6", "display": "Yes"},
                {"system": "http://loinc.org", "code": "LA32-8", "display": "No"},
                {"system": "http://loinc.org", "code": "LA4489-6", "display": "Unknown"},
            ],
        },
        {
            "link_id": "SNM/419099009",
            "text": "Is this test for a deceased infant?",
            "value_type": "codeable_concept",
            "code": {"system": "http://snomed.info/sct", "code": "419099009", "display": "Dead"},
            "options": [
                {"system": "http://loinc.org", "code": "LA33-6", "display": "Yes"},
                {"system": "http://loinc.org", "code": "LA32-8", "display": "No"},
                {"system": "http://loinc.org", "code": "LA4489-6", "display": "Unknown"},
            ],
        },
    ]

    @classmethod
    def _build_aoe_observation(cls, question, raw_value, patient_ref, authored_on):
        """One Ask-At-Order-Entry answer as an Observation resource (see
        ASK_AT_ORDER_ENTRY_QUESTIONS), `subject` referencing the
        in-bundle Patient via its urn:uuid `patient_ref`. `raw_value` is
        the submitted form value — an option code for a `codeable_concept`
        question, an ISO date string for `date_time`, or a plain number
        for `quantity`. `authored_on` becomes `effectiveDateTime` — the
        same value as this order's own `ServiceRequest.authoredOn`, since
        these Observations only exist to answer questions asked at the
        moment this order was placed."""
        observation = {
            "resourceType": "Observation",
            "effectiveDateTime": authored_on,
            # Observation.identifier is 1..1 mandatory per the IG's
            # ObservationOrder profile
            # (https://nw-gmsa.github.io/en/StructureDefinition-ObservationOrder.html)
            # -- confirmed by a real validation failure against a live
            # server, not just a profile technicality. A bare generated
            # UUID with no `system`, same shape a real producer's export
            # uses for these (examples/Liverpool_O21_Apr26.json's OBX-*
            # Observations, e.g.
            # {"value": "54bbd361-474d-4090-9727-aaf1b7d1bacd"}) -- this
            # app's own worked example
            # (examples/genomic-order-YHCRABCDORDER.json) omits it
            # entirely, which is what let this slip through unnoticed.
            "identifier": [{"value": str(uuid.uuid4())}],
            "status": "final",
            "category": [{"coding": [
                {"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"},
            ]}],
            "code": {"coding": [dict(question["code"])]},
            "subject": {"reference": patient_ref},
        }
        if question["value_type"] == "codeable_concept":
            option = next((o for o in question.get("options", []) if o["code"] == raw_value), None)
            observation["valueCodeableConcept"] = {"coding": [dict(option) if option else {"code": raw_value}]}
        elif question["value_type"] == "date_time":
            observation["valueDateTime"] = raw_value
        elif question["value_type"] == "quantity":
            quantity = {"value": float(raw_value)}
            quantity.update(question.get("unit") or {})
            observation["valueQuantity"] = quantity
        return observation

    @staticmethod
    def _format_gmc_number(value):
        """Normalizes a GMC number to this IG's required identifier
        *value* format — literally the letter "C" followed by the
        digits (e.g. "C3456789"), per
        https://nw-gmsa.github.io/en/StructureDefinition-PractitionerIdentifier.html#professional-registration-entry-identifier
        ("CONSULTANT_CODE", format `CNNNNNNN`) — confirmed by that page's
        own worked example, `{"system": "https://fhir.hl7.org.uk/Id/gmc-number",
        "value": "C3456789"}`. Strips any existing "C"/"c" prefix first
        and re-adds exactly one, so this is safe to call whether the
        Practitioner resource already has it prefixed or not (e.g.
        import_econcur() currently stores the bare digits). Returns None
        unchanged if `value` is falsy."""
        if not value:
            return None
        digits = value.strip().lstrip("Cc").strip()
        return f"C{digits}" if digits else None

    @staticmethod
    def _logical_reference(system, value, display=None):
        """A FHIR logical reference — `identifier` + optional `display`,
        deliberately no `.reference` — for linking to a resource that
        isn't included in this bundle and doesn't live on the receiving
        system's server either. Mirrors exactly how the IG's own worked
        example links PractitionerRole.practitioner/.organization (by
        GMC number / ODS code) and MessageHeader.sender — a bundle meant
        to travel to another system can't reference our internal
        database ids, which would be meaningless there."""
        ref = {"identifier": {"system": system, "value": value}}
        if display:
            ref["display"] = display
        return ref

    #: Fixed message destination for orders built by this screen — this
    #: app is specifically for the North West Genomic Laboratory Hub, so
    #: every order goes to the same place. Taken directly from the IG's
    #: own worked example
    #: (https://nw-gmsa.github.io/en/Bundle-GenomicsOrderMessageCodedEntries.html),
    #: not guessed.
    ORDER_MESSAGE_DESTINATION_ENDPOINT = "https://fhir.nwgenomics.nhs.uk/Endpoint/RIE"
    ORDER_MESSAGE_DESTINATION_ODS = "699X0"
    ORDER_MESSAGE_DESTINATION_NAME = "NORTH WEST GLH"

    #: This app has no registered Endpoint resource of its own (unlike
    #: the worked example's sending system, which has a real
    #: "https://fhir.nwgenomics.nhs.uk/Endpoint/EPIC") — MessageHeader.source.endpoint
    #: is mandatory per FHIR R4, so this is a placeholder rather than an
    #: omission; not confirmed/registered anywhere.
    ORDER_MESSAGE_SOURCE_ENDPOINT = "https://fhir.nwgenomics.nhs.uk/Endpoint/LabExplorer"

    def _patient_for_order_bundle(self, patient, organization_ods, hospital_number=None):
        """The Patient resource to inline into the order message bundle
        — a shallow copy of `patient` with its `identifier` list adjusted
        for the requesting organisation (`organization_ods`):

        - Medical record number (HL7 v2-0203 "MR") identifiers assigned
          by a *different* organisation are dropped — a receiving lab
          has no use for, and shouldn't be sent, this patient's hospital
          number at some unrelated trust. Non-MR identifiers (NHS
          number, etc.) are never touched.
        - If `hospital_number` is given (the order form's "Hospital
          number" field — pre-filled from any existing MR identifier
          already assigned by this organisation, see
          medical_record_numbers()), it replaces whatever MR identifier
          this organisation already had on the resource, in case the
          form value was edited from what was pre-filled.

        `resolve_organisation_ods()` (not organisation_ods_code()) is
        used to read each identifier's own assigner, since — same as
        medical_record_numbers() — `identifier.assigner` can be a
        logical reference (inline `.identifier`, no resource to fetch)
        or a literal one needing a GET, and this server uses either
        shape depending on the identifier.
        """
        identifiers = []
        for ident in patient.get("identifier", []):
            codings = (ident.get("type") or {}).get("coding", [])
            is_mr = any(c.get("code") == self.MEDICAL_RECORD_NUMBER_TYPE for c in codings)
            if is_mr:
                assigner_ods = self.resolve_organisation_ods(ident.get("assigner") or {})
                if assigner_ods != organization_ods or hospital_number:
                    continue
            identifiers.append(ident)
        if hospital_number:
            new_mrn = {
                "type": {"coding": [{"system": self.IDENTIFIER_TYPE_SYSTEM, "code": self.MEDICAL_RECORD_NUMBER_TYPE}]},
                "value": hospital_number,
            }
            if organization_ods:
                new_mrn["assigner"] = self._logical_reference(self.ODS_ORGANIZATION_CODE_SYSTEM, organization_ods)
            identifiers.append(new_mrn)
        return {**patient, "identifier": identifiers}

    #: The IG's own published specimen-type ValueSet
    #: (https://nw-gmsa.github.io/en/ValueSet-specimen-type.html) —
    #: SNOMED-coded specimen types only; the ValueSet also includes an
    #: open-ended "all codes in https://fhir.nwgenomics.nhs.uk/CodeSystem/IGENE"
    #: rule for backward-compatible local codes, but the page's own text
    #: says "SNOMED codes are preferred" and that CodeSystem's contents
    #: aren't published anywhere this app can enumerate, so only the 24
    #: SNOMED concepts are offered here. Hardcoded (not fetched live)
    #: since — unlike GENOMIC_TEST_DIRECTORY_CODESYSTEM_URL — the
    #: ValueSet's own JSON doesn't carry `display` text for most of these
    #: concepts (only the rendered HTML expansion does), so a live fetch
    #: wouldn't save anything here.
    SPECIMEN_TYPE_VALUESET_URL = "https://fhir.nwgenomics.nhs.uk/ValueSet/specimen-type"
    SPECIMEN_TYPE_CODES = [
        {"code": "119297000", "display": "Blood specimen"},
        {"code": "258580003", "display": "Whole blood specimen"},
        {"code": "122552005", "display": "Arterial blood specimen"},
        {"code": "122555007", "display": "Venous blood specimen"},
        {"code": "122556008", "display": "Cord blood specimen"},
        {"code": "737357006", "display": "Fetal blood specimen"},
        {"code": "440500007", "display": "Dried blood spot specimen"},
        {"code": "119359002", "display": "Bone marrow specimen"},
        {"code": "119373006", "display": "Amniotic fluid specimen"},
        {"code": "258565009", "display": "Chorionic villi specimen"},
        {"code": "309201001", "display": "Ascitic fluid specimen"},
        {"code": "258450006", "display": "Cerebrospinal fluid specimen"},
        {"code": "122571007", "display": "Pericardial fluid specimen"},
        {"code": "418564007", "display": "Pleural fluid specimen"},
        {"code": "309147000", "display": "Thyroid cyst fluid specimen"},
        {"code": "119342007", "display": "Saliva specimen"},
        {"code": "122575003", "display": "Urine specimen"},
        {"code": "733104004", "display": "Swab from buccal mucosa"},
        {"code": "441479001", "display": "Fresh tissue specimen"},
        {"code": "441652008", "display": "Formalin-fixed paraffin-embedded tissue specimen"},
        {"code": "702451000", "display": "Cultured cells"},
        {"code": "258566005", "display": "Deoxyribonucleic acid specimen"},
        {"code": "441673008", "display": "Ribonucleic acid specimen"},
        {"code": "1003517007", "display": "Freeze dried specimen"},
    ]

    @classmethod
    def specimen_type_display(cls, code):
        """Display text for a SPECIMEN_TYPE_CODES `code`, or the bare
        code itself if it's somehow not in the list — same
        trust-the-source-list pattern as genomic_test_directory_display()."""
        for entry in cls.SPECIMEN_TYPE_CODES:
            if entry["code"] == code:
                return entry["display"]
        return code

    def build_order_message_bundle(
        self, *, patient, organization, practitioner, hospital_number=None,
        hospital_spell_id=None,
        test_code, order_number=None, priority="routine", clinical_details=None,
        specimen_type, specimen_date=None, specimen_received_date=None,
        specimen_placer_id=None, specimen_accession_number=None,
        specimen_tracking_number=None, aoe_answers=None, extra_observations=None,
    ):
        """
        Builds a genomic test order as a FHIR message Bundle
        (`Bundle.type = "message"`: a `MessageHeader` + the resources it
        `focus`es on) for the order-create screen (app.order_new()) to
        offer as a `.json` download — shaped after the IG's own worked
        example at
        https://nw-gmsa.github.io/en/Bundle-GenomicsOrderMessageCodedEntries.html.

        **Nothing this method builds is written to the FHIR server** —
        no `_post`/`_put` calls anywhere in it. `patient`/`organization`/
        `practitioner` are the full resources already resolved from this
        server (searched/picked, never freely typed — see
        app.order_new()), not just ids, because a message bundle must be
        self-contained: a receiving system has no way to dereference a
        "Patient/<our-internal-id>" reference, so this inlines a full
        copy of the picked Patient (see _patient_for_order_bundle() for
        how its identifier list is adjusted first) and links the picked
        Practitioner/Organization by *identifier* (GMC number / ODS
        code) rather than by internal id — `_logical_reference()`,
        exactly how the worked example's own PractitionerRole entry (and
        MessageHeader.sender) does it. Every resource in the bundle gets
        a fresh `urn:uuid:` `fullUrl`; none of them have (or need) a
        real server-assigned id.

        `hospital_number`, if given, becomes this Patient's medical
        record number (HL7 v2-0203 "MR") *for the requesting
        organisation* — see _patient_for_order_bundle().

        `hospital_spell_id`, if given, becomes `ServiceRequest.encounter`
        — a minimal Encounter resource carrying it as an HL7 v2-0203 "AN"
        (Account number) identifier, assigned by the requesting
        organisation — this form treats the value as an account number
        specifically (the term e.g. Liverpool Women's/Alder Hey use for
        it — see hospital_spell_identifier()'s own docstring), rather
        than the "VN" (Visit number) coding a real producer's export
        (examples/Liverpool_O21_Apr26.json) happens to use; either way
        it's read back the same by hospital_spell_identifier() elsewhere
        in this file, which recognizes both.

        `test_code` must be a code from genomic_test_directory_codes()
        — its display text is looked up from there
        (genomic_test_directory_display()) rather than trusting a
        second free-typed field, so ServiceRequest.code/.text always
        match what the R-code select actually offered.

        `aoe_answers`, if given, is `{link_id: raw form value}` for
        whichever of ASK_AT_ORDER_ENTRY_QUESTIONS were answered — each
        becomes its own Observation entry (_build_aoe_observation()),
        referenced from ServiceRequest.supportingInfo, same as the
        worked example's OBX-* Observations for these same questions.

        `extra_observations`, if given, is a list of
        `{"label", "value"}` dicts — parse_order_message_bundle()'s own
        "extra observations" (ones from a loaded order that didn't match
        any ASK_AT_ORDER_ENTRY_QUESTIONS) round-tripped back out as
        simple `Observation`s (`code.text` = label, `valueString` =
        value), also referenced from `ServiceRequest.supportingInfo`.
        Loading an order and re-submitting it without this would
        silently drop whatever supplementary data didn't fit one of the
        7 fixed questions — this is what keeps it in the output instead
        of only ever showing it read-only on the form.

        `reasonCode` (the clinical indication) is always derived from
        `test_code` itself — the part before the "." — rather than a
        separate parameter; see genomic_clinical_indications().

        `specimen_type` is required — it must be a code from
        SPECIMEN_TYPE_CODES — since the IG's Specimen profile makes
        `Specimen.type` mandatory (1..1); every other `specimen_*`
        parameter maps onto fields listed under that profile's own
        "Domain Archetype" table
        (https://nw-gmsa.github.io/en/StructureDefinition-Specimen.html#domain-archetype):
        `specimen_placer_id` → `Specimen.identifier[PlacerSpecimenNumber]`,
        `specimen_accession_number` → `Specimen.accessionIdentifier`,
        `specimen_tracking_number` → `Specimen.identifier[ShipmentTrackingNumber]`
        (LOINC 97209-1, confirmed by that same table), `specimen_date` →
        `Specimen.collection.collectedDateTime`, `specimen_received_date`
        → `Specimen.receivedTime`. Source site/notes (which that table
        also lists) aren't collected by the form.

        One order = one test, one specimen — the paper form's note that
        "more than one Test Indication Code can be requested" would need
        multiple ServiceRequest resources (optionally sharing one
        Specimen); not built here, submit the form again for a second
        test. Fields the paper form has that neither this IG's profiles
        nor the domain archetype/AskAtOrderEntry questions model
        structurally fold into `clinical_details` free text instead, same
        faithful-subset-only approach order_view.html documents for
        reading real orders back.
        """
        aoe_answers = aoe_answers or {}

        def new_ref():
            return f"urn:uuid:{uuid.uuid4()}"

        entries = []

        # Computed once and reused for both ServiceRequest.authoredOn and
        # every Observation's effectiveDateTime below, so they always
        # agree rather than each grabbing today() independently (which
        # could disagree if the two happened to straddle midnight).
        authored_on = date.today().isoformat()

        organization_ods = self.organisation_ods_code(organization)
        organization_name = organization.get("name")

        patient_ref = new_ref()
        entries.append({
            "fullUrl": patient_ref,
            "resource": self._patient_for_order_bundle(patient, organization_ods, hospital_number),
        })

        # GMC values on a stored Practitioner may or may not already
        # carry the "C" prefix this IG's identifier format requires (see
        # _format_gmc_number()) — normalized here so the exported bundle
        # is spec-correct regardless of how it's stored on this server.
        practitioner_gmc = self._format_gmc_number(self._identifier_value(practitioner, self.GMC_NUMBER_SYSTEM))
        practitioner_name = self._practitioner_name(practitioner)

        practitioner_role_ref = new_ref()
        entries.append({"fullUrl": practitioner_role_ref, "resource": {
            "resourceType": "PractitionerRole",
            "practitioner": (
                self._logical_reference(self.GMC_NUMBER_SYSTEM, practitioner_gmc, practitioner_name)
                if practitioner_gmc else {"display": practitioner_name}
            ),
            "organization": (
                self._logical_reference(self.ODS_ORGANIZATION_CODE_SYSTEM, organization_ods, organization_name)
                if organization_ods else {"display": organization_name}
            ),
        }})

        specimen_ref = new_ref()
        specimen_identifiers = []
        if specimen_placer_id:
            placer_specimen_id = {"value": specimen_placer_id}
            if organization_ods:
                placer_specimen_id["assigner"] = self._logical_reference(
                    self.ODS_ORGANIZATION_CODE_SYSTEM, organization_ods, organization_name)
            specimen_identifiers.append(placer_specimen_id)
        if specimen_tracking_number:
            specimen_identifiers.append({
                "type": {"coding": [{"system": "http://loinc.org", "code": "97209-1", "display": "Shipment tracking number"}]},
                "value": specimen_tracking_number,
            })
        specimen = {
            "resourceType": "Specimen",
            "subject": {"reference": patient_ref},
            "type": {"coding": [{
                "system": "http://snomed.info/sct", "code": specimen_type,
                "display": self.specimen_type_display(specimen_type),
            }]},
        }
        if specimen_identifiers:
            specimen["identifier"] = specimen_identifiers
        if specimen_accession_number:
            specimen["accessionIdentifier"] = {"value": specimen_accession_number}
        if specimen_date:
            specimen["collection"] = {"collectedDateTime": specimen_date}
        if specimen_received_date:
            specimen["receivedTime"] = specimen_received_date
        entries.append({"fullUrl": specimen_ref, "resource": specimen})

        encounter_ref = None
        if hospital_spell_id:
            encounter_identifier = {
                "type": {"coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                    "code": "AN", "display": "Account number",
                }]},
                "value": hospital_spell_id,
            }
            if organization_ods:
                encounter_identifier["assigner"] = self._logical_reference(
                    self.ODS_ORGANIZATION_CODE_SYSTEM, organization_ods, organization_name)
            encounter_ref = new_ref()
            entries.append({"fullUrl": encounter_ref, "resource": {
                "resourceType": "Encounter",
                "status": "finished",
                # Encounter.class is 1..1 mandatory per the base FHIR R4
                # spec itself (not just the IG) — confirmed by a real
                # validation failure ("Encounter.class: minimum required
                # = 1, but only found 0") against a live server. Neither
                # real producer example in examples/ populates it either
                # (this form doesn't collect an encounter type at all),
                # so "AMB" (ambulatory) is a best-effort default rather
                # than a confirmed value — the standard v3-ActCode
                # fallback when the actual class is unknown.
                "class": {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                    "code": "AMB", "display": "ambulatory",
                },
                "subject": {"reference": patient_ref},
                "identifier": [encounter_identifier],
            }})

        supporting_info = []
        for question in self.ASK_AT_ORDER_ENTRY_QUESTIONS:
            raw_value = aoe_answers.get(question["link_id"])
            if not raw_value:
                continue
            obs_ref = new_ref()
            entries.append({"fullUrl": obs_ref, "resource": self._build_aoe_observation(question, raw_value, patient_ref, authored_on)})
            supporting_info.append({"reference": obs_ref})

        for extra in extra_observations or []:
            if not extra.get("value") or extra["value"] == "—":
                continue
            obs_ref = new_ref()
            entries.append({"fullUrl": obs_ref, "resource": {
                "resourceType": "Observation",
                # Same ObservationOrder-profile requirement as
                # _build_aoe_observation() -- see its comment.
                "identifier": [{"value": str(uuid.uuid4())}],
                "status": "final",
                "effectiveDateTime": authored_on,
                "category": [{"coding": [
                    {"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"},
                ]}],
                "code": {"text": extra.get("label") or "—"},
                "subject": {"reference": patient_ref},
                "valueString": extra["value"],
            }})
            supporting_info.append({"reference": obs_ref})

        # order_number lets the form supply a placer number from an
        # external ordering system instead of one this app mints itself
        # — same ORDER_PLACER_NUMBER_SYSTEM/PLAC shape either way, so
        # placer_identifier() reads either back identically.
        placer_identifier = {
            "system": self.ORDER_PLACER_NUMBER_SYSTEM,
            "value": order_number or self.generate_order_placer_number(),
            "type": {"coding": [{"system": self.IDENTIFIER_TYPE_SYSTEM, "code": self.PLACER_IDENTIFIER_TYPE}]},
        }
        if organization_ods:
            placer_identifier["assigner"] = self._logical_reference(
                self.ODS_ORGANIZATION_CODE_SYSTEM, organization_ods, organization_name)

        test_display = self.genomic_test_directory_display(test_code)
        indication_code = test_code.split(".")[0]
        indication_display = self.genomic_clinical_indication_display(indication_code)
        order = {
            "resourceType": "ServiceRequest",
            "status": "active",
            "intent": "order",
            "category": [{"coding": [
                {"system": "http://snomed.info/sct", "code": "116148004", "display": "Genomic procedure"},
            ]}],
            "priority": priority,
            "code": {
                "coding": [{"system": self.GENOMIC_TEST_DIRECTORY_SYSTEM, "code": test_code, "display": test_display}],
                "text": test_display,
            },
            "subject": {"reference": patient_ref},
            "requester": {"reference": practitioner_role_ref},
            "authoredOn": authored_on,
            "reasonCode": [{"coding": [
                {"system": self.GENOMIC_CLINICAL_INDICATION_SYSTEM, "code": indication_code, "display": indication_display},
            ]}],
            "identifier": [placer_identifier],
        }
        if clinical_details:
            order["note"] = [{"text": clinical_details}]
        order["specimen"] = [{"reference": specimen_ref}]
        if encounter_ref:
            order["encounter"] = {"reference": encounter_ref}
        if supporting_info:
            order["supportingInfo"] = supporting_info

        service_request_ref = new_ref()
        entries.append({"fullUrl": service_request_ref, "resource": order})

        message_header = {
            "resourceType": "MessageHeader",
            "eventCoding": {
                "system": "http://terminology.hl7.org/CodeSystem/v2-0003",
                "code": "O21",
                "display": "OML - Laboratory order",
            },
            "destination": [{
                "endpoint": self.ORDER_MESSAGE_DESTINATION_ENDPOINT,
                "receiver": self._logical_reference(
                    self.ODS_ORGANIZATION_CODE_SYSTEM,
                    self.ORDER_MESSAGE_DESTINATION_ODS, self.ORDER_MESSAGE_DESTINATION_NAME),
            }],
            "sender": (
                self._logical_reference(self.ODS_ORGANIZATION_CODE_SYSTEM, organization_ods, organization_name)
                if organization_ods else {"display": organization_name}
            ),
            "source": {"software": "Lab Explorer", "endpoint": self.ORDER_MESSAGE_SOURCE_ENDPOINT},
            "focus": [{"reference": service_request_ref, "type": "ServiceRequest"}],
        }
        entries.insert(0, {"fullUrl": new_ref(), "resource": message_header})

        return {
            "resourceType": "Bundle",
            "type": "message",
            "identifier": {"system": "urn:ietf:rfc:3986", "value": f"urn:uuid:{uuid.uuid4()}"},
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "entry": entries,
        }

    # ------------------------------------------------------------------
    # ESB submission — /order/new's "Send to ESB" action (app.py)
    #
    # Sends a build_order_message_bundle() Bundle straight to this
    # deployment's ESB (Enterprise Service Bus) $process-message
    # endpoint instead of downloading it — a genuinely separate
    # integration from the rest of this file: it talks to a different
    # base URL, with its own OAuth2 client-credentials app registration,
    # not the per-user Basic-auth FhirClient session everything else in
    # this app uses. Kept as classmethods (no FhirClient instance/session
    # needed) with a class-level token cache, same reasoning as
    # _icb_boundary_cache/_genomic_test_directory_cache above: this
    # doesn't depend on which user is logged in.
    # ------------------------------------------------------------------

    #: The two URLs are this deployment's actual values (not secrets) and
    #: so have defaults; ESB_CLIENT_ID/ESB_CLIENT_SECRET below deliberately
    #: don't — they're credentials, only ever read from the environment,
    #: never hardcoded or committed. Same host as FHIR_BASE_URL's own
    #: default (192.168.1.62) — see esb_config() for why FHIR_VERIFY_SSL
    #: is reused for this endpoint's TLS verification too.
    ESB_TOKEN_URL_DEFAULT = "https://192.168.1.62/healthconnect/oauth2/token"
    ESB_PROCESS_MESSAGE_URL_DEFAULT = "https://192.168.1.62/healthconnect/ESB/$process-message"

    #: Class-level OAuth2 token cache — not per-request, since a client-
    #: credentials token is valid for every request until it expires
    #: regardless of who's using this app.
    _esb_token_cache = {"access_token": None, "expires_at": 0}

    @classmethod
    def esb_config(cls):
        """(token_url, process_message_url, client_id, client_secret,
        verify_ssl, scope) — read fresh from the environment on every
        call (ESB_TOKEN_URL/ESB_PROCESS_MESSAGE_URL/ESB_CLIENT_ID/
        ESB_CLIENT_SECRET/ESB_SCOPE), same lazy-env-var pattern
        FhirClient.__init__ uses for FHIR_BASE_URL/FHIR_USER/
        FHIR_PASSWORD, rather than freezing them at import time.
        `verify_ssl` reuses FHIR_VERIFY_SSL rather than a separate env
        var — the ESB endpoint is the same host (192.168.1.62) as this
        deployment's own FHIR_BASE_URL, so the same TLS cert situation
        applies. `scope` is None unless ESB_SCOPE is set — many OAuth2
        authorization servers (InterSystems IRIS/HealthConnect's
        included) reject a client_credentials token request with 400
        `invalid_scope`/`invalid_request` if no `scope` is sent at all,
        so this is opt-in per deployment rather than guessed at."""
        return (
            os.environ.get("ESB_TOKEN_URL", cls.ESB_TOKEN_URL_DEFAULT),
            os.environ.get("ESB_PROCESS_MESSAGE_URL", cls.ESB_PROCESS_MESSAGE_URL_DEFAULT),
            os.environ.get("ESB_CLIENT_ID"),
            os.environ.get("ESB_CLIENT_SECRET"),
            os.environ.get("FHIR_VERIFY_SSL", "false").lower() == "true",
            os.environ.get("ESB_SCOPE"),
        )

    @classmethod
    def esb_access_token(cls, force_refresh=False):
        """OAuth2 client-credentials bearer token for the ESB (see
        esb_config()) — client id/secret sent as HTTP Basic auth on the
        token request, per this deployment's own instructions (not every
        OAuth2 server does it this way; some expect them in the POST
        body instead). Cached at class level until 60s before it expires,
        so a burst of sends doesn't re-authenticate every time;
        `force_refresh=True` bypasses the cache (send_order_to_esb()
        uses this to recover from a 401 on an apparently-still-valid
        cached token — expired early, or revoked server-side).

        Raises RuntimeError if ESB_CLIENT_ID/ESB_CLIENT_SECRET aren't
        set — this app never has default/hardcoded credentials for this
        endpoint, unlike the two URLs above. Raises requests.HTTPError
        with the authorization server's own error body (not just the
        generic "400 Client Error: Bad Request" requests.Response.
        raise_for_status() gives on its own — OAuth2 token errors are
        JSON per RFC 6749 §5.2, `{"error": "...", "error_description":
        "..."}`, and that detail is the only way to actually tell
        invalid_client from invalid_scope from anything else) if the
        token request itself is rejected.
        """
        token_url, _, client_id, client_secret, verify_ssl, scope = cls.esb_config()
        if not client_id or not client_secret:
            raise RuntimeError(
                "ESB_CLIENT_ID / ESB_CLIENT_SECRET are not set as environment variables "
                "— required to authenticate to the ESB before an order can be sent."
            )
        cached = cls._esb_token_cache
        if not force_refresh and cached["access_token"] and time.time() < cached["expires_at"]:
            return cached["access_token"]
        data = {"grant_type": "client_credentials"}
        if scope:
            data["scope"] = scope
        resp = requests.post(
            token_url, data=data,
            auth=HTTPBasicAuth(client_id, client_secret),
            timeout=15, verify=verify_ssl,
        )
        if not resp.ok:
            detail = None
            try:
                body = resp.json()
                detail = body.get("error_description") or body.get("error")
            except ValueError:
                detail = (resp.text or "").strip()[:500] or None
            message = f"ESB token request to {token_url} failed ({resp.status_code})"
            if detail:
                message += f": {detail}"
            elif not scope:
                message += " (no ESB_SCOPE set — try setting one if the server requires it)"
            raise requests.HTTPError(message, response=resp)
        token_data = resp.json()
        access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 300)
        cls._esb_token_cache = {"access_token": access_token, "expires_at": time.time() + max(expires_in - 60, 0)}
        return access_token

    @staticmethod
    def _post_bundle_to_esb(url, bundle, token, verify_ssl):
        return requests.post(
            url, json=bundle,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/fhir+json",
                "Accept": "application/fhir+json",
            },
            timeout=30, verify=verify_ssl,
        )

    @classmethod
    def send_order_to_esb(cls, bundle):
        """POSTs `bundle` (a build_order_message_bundle() message
        Bundle) to the ESB's $process-message endpoint
        (ESB_PROCESS_MESSAGE_URL, see esb_config()), Bearer-authenticated
        via esb_access_token(). One retry with a forced token refresh on
        a 401 — the one client-credentials failure worth silently
        recovering from (a cached token that expired early or was
        revoked server-side); any other failure (network error, 4xx/5xx
        after the retry) raises via `raise_for_status()` for the caller
        to show as-is, same "surface it, don't swallow it" approach this
        app takes everywhere else.

        Returns the parsed JSON response body (typically another message
        Bundle — the $process-message response/acknowledgement), or None
        if the response has no body / isn't JSON.
        """
        _, process_message_url, _, _, verify_ssl, _ = cls.esb_config()
        token = cls.esb_access_token()
        resp = cls._post_bundle_to_esb(process_message_url, bundle, token, verify_ssl)
        if resp.status_code == 401:
            token = cls.esb_access_token(force_refresh=True)
            resp = cls._post_bundle_to_esb(process_message_url, bundle, token, verify_ssl)
        resp.raise_for_status()
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    @classmethod
    def parse_order_message_bundle(cls, bundle):
        """
        Extracts order-create-screen prefill values from a previously
        obtained genomic test order message Bundle — the same shape
        build_order_message_bundle() produces (and a real downloaded
        order, e.g. examples/genomic-order-YHCRABCDORDER.json, has):
        MessageHeader + inline Patient + PractitionerRole (logical
        references) + Specimen + Observations + ServiceRequest, all
        linked by `urn:uuid:` `fullUrl`.

        Patient/organisation/clinician still can't be filled in
        directly — app.order_new()'s "always picked from this FHIR
        server" rule doesn't change just because a file was loaded — so
        this only extracts their *identifying* values (NHS number, ODS
        code, GMC number) for the caller to search this server with
        (search_patients()/search_organizations()/
        practitioners_for_organization()). Everything else (test code,
        specimen fields, priority, clinical details, AOE answers) comes
        back ready to drop straight into form_values/aoe_values.

        Raises ValueError if `bundle` isn't a Bundle containing a
        ServiceRequest — this is meant for a bundle this same screen (or
        something producing the same shape) built, not arbitrary FHIR.
        """
        if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
            raise ValueError("Not a FHIR Bundle.")

        resources_by_ref = {
            entry["fullUrl"]: entry["resource"]
            for entry in bundle.get("entry", [])
            if entry.get("fullUrl") and entry.get("resource")
        }

        def resolve(ref):
            return resources_by_ref.get((ref or {}).get("reference")) if ref else None

        service_request = next(
            (r for r in resources_by_ref.values() if r.get("resourceType") == "ServiceRequest"), None)
        if not service_request:
            raise ValueError("No ServiceRequest found in this bundle.")

        extracted = {}

        patient = resolve(service_request.get("subject"))
        organization_ods = None
        if patient:
            extracted["patient_nhs_number"] = cls.nhs_number(patient)
            extracted["patient_name"] = cls._practitioner_name(patient)

        # ServiceRequest.requester isn't always a PractitionerRole
        # reference into this same bundle — a real producer (Liverpool's
        # O21 export, examples/Liverpool_O21_Apr26.json) sends it as a
        # bare logical reference straight to the requesting Organization
        # instead ({"identifier": {ods-organization-code, ...}}, no
        # `.reference`, no individually identified clinician anywhere in
        # the bundle at all) — the same "requester isn't always a
        # PractitionerRole" case requester_display() already handles on
        # the read side (see "Reference resolution" elsewhere in this
        # file). Handled here as three possibilities, broadest first:
        requester_ref = service_request.get("requester") or {}
        requester_resource = resolve(requester_ref)
        if requester_resource and requester_resource.get("resourceType") == "PractitionerRole":
            org_ref = requester_resource.get("organization") or {}
            organization_ods = (org_ref.get("identifier") or {}).get("value")
            extracted["organization_ods"] = organization_ods
            extracted["organization_name"] = org_ref.get("display")
            practitioner_ref = requester_resource.get("practitioner") or {}
            gmc = (practitioner_ref.get("identifier") or {}).get("value")
            if gmc:
                extracted["practitioner_gmc"] = gmc
                extracted["practitioner_name"] = practitioner_ref.get("display")
        elif requester_resource and requester_resource.get("resourceType") == "Organization":
            organization_ods = cls.organisation_ods_code(requester_resource)
            extracted["organization_ods"] = organization_ods
            extracted["organization_name"] = requester_resource.get("name")
        elif requester_ref.get("identifier"):
            organization_ods = requester_ref["identifier"].get("value")
            extracted["organization_ods"] = organization_ods
            extracted["organization_name"] = requester_ref.get("display")

        if patient and organization_ods:
            for ident in patient.get("identifier", []):
                codings = (ident.get("type") or {}).get("coding", [])
                if not any(c.get("code") == cls.MEDICAL_RECORD_NUMBER_TYPE for c in codings):
                    continue
                assigner_ods = ((ident.get("assigner") or {}).get("identifier") or {}).get("value")
                if assigner_ods == organization_ods:
                    extracted["hospital_number"] = ident.get("value")
                    break

        # Hospital spell identifier — same "prefer AN/VN-typed, else the
        # first identifier with a value" resolution as
        # hospital_spell_identifier() (which returns a formatted display
        # string; this needs the bare value for the form field instead).
        encounter = resolve(service_request.get("encounter"))
        if encounter:
            preferred, fallback = None, None
            for ident in encounter.get("identifier", []):
                if not ident.get("value"):
                    continue
                type_codes = [c.get("code") for c in (ident.get("type") or {}).get("coding", [])]
                if any(code in cls.HOSPITAL_SPELL_IDENTIFIER_TYPES for code in type_codes):
                    preferred = ident
                    break
                if fallback is None:
                    fallback = ident
            chosen = preferred or fallback
            if chosen:
                extracted["hospital_spell_id"] = chosen.get("value")

        extracted["test_code"] = cls.test_directory_code(service_request.get("code"))
        extracted["priority"] = service_request.get("priority") or "routine"
        # Joins every note, not just the first — a real producer's
        # export (examples/Liverpool_O21_Apr26.json) puts each
        # order-entry-form field into its own separate `note` entry
        # (27 of them, one per field: "**Referring Clinician Name:**
        # : _Dr Natalie Canham_" and so on) rather than one combined
        # block, so taking only note[0] silently dropped the rest.
        note_texts = [n["text"] for n in (service_request.get("note") or []) if n.get("text")]
        if note_texts:
            extracted["clinical_details"] = "\n".join(note_texts)
        order_number = cls.placer_identifier(service_request)
        if order_number:
            extracted["order_number"] = order_number

        specimen_refs = service_request.get("specimen") or []
        specimen = resolve(specimen_refs[0]) if specimen_refs else None
        if not specimen:
            # A real producer's export (examples/Liverpool_O21_Apr26.json)
            # includes a Specimen resource in the bundle but never
            # references it from ServiceRequest.specimen at all — same
            # "servers don't reliably link things the way the spec
            # implies" lesson as ctdna_orders()'s Bundle.entry.search.mode
            # caveat elsewhere in this file. Falls back to the Specimen
            # (if exactly one) whose own `subject` matches this order's
            # patient, rather than leaving specimen_type/etc. unset.
            subject_ref = (service_request.get("subject") or {}).get("reference")
            candidates = [
                r for r in resources_by_ref.values()
                if r.get("resourceType") == "Specimen" and subject_ref
                and (r.get("subject") or {}).get("reference") == subject_ref
            ]
            if len(candidates) == 1:
                specimen = candidates[0]
        if specimen:
            for coding in (specimen.get("type") or {}).get("coding", []):
                if coding.get("system") == "http://snomed.info/sct":
                    extracted["specimen_type"] = coding.get("code")
                    break
            collection = specimen.get("collection") or {}
            if collection.get("collectedDateTime"):
                extracted["specimen_date"] = collection["collectedDateTime"][:10]
            if specimen.get("receivedTime"):
                extracted["specimen_received_date"] = specimen["receivedTime"][:10]
            accession_value = (specimen.get("accessionIdentifier") or {}).get("value")
            if accession_value:
                extracted["specimen_accession_number"] = accession_value
            for ident in specimen.get("identifier", []):
                type_codes = [c.get("code") for c in (ident.get("type") or {}).get("coding", [])]
                if "97209-1" in type_codes:
                    extracted["specimen_tracking_number"] = ident.get("value")
                elif not ident.get("type"):
                    extracted["specimen_placer_id"] = ident.get("value")

        def is_observation_panel(observation):
            """Whether `observation` is a grouping/"panel" Observation —
            just a `hasMember` list pointing at the real ones, no code
            or value of its own (examples/Liverpool_O21_Apr26.json's
            single supportingInfo entry is exactly this: 14 real
            Observations grouped under one panel with nothing but
            `hasMember`) — as opposed to a leaf Observation that merely
            also happens to reference others."""
            has_own_code = bool(((observation.get("code") or {}).get("coding")) or (observation.get("code") or {}).get("text"))
            has_own_value = any(key.startswith("value") for key in observation)
            return bool(observation.get("hasMember")) and not has_own_code and not has_own_value

        def flatten_observation_refs(refs):
            """Expands `refs` (a list of Observation References, e.g.
            ServiceRequest.supportingInfo) into (reference_url,
            observation) pairs for the *leaf* Observations they resolve
            to — dereferencing through any panel Observation's
            `hasMember` (recursively, in case a panel groups other
            panels) rather than yielding the panel itself, which has no
            code/value to match against ASK_AT_ORDER_ENTRY_QUESTIONS."""
            leaves = []
            for ref in refs or []:
                obs = resolve(ref)
                if not obs or obs.get("resourceType") != "Observation":
                    continue
                if is_observation_panel(obs):
                    leaves.extend(flatten_observation_refs(obs.get("hasMember")))
                else:
                    leaves.append(((ref or {}).get("reference"), obs))
            return leaves

        aoe_values = {}
        matched_observation_urls = set()
        for obs_ref_url, obs in flatten_observation_refs(service_request.get("supportingInfo")):
            obs_coding = ((obs.get("code") or {}).get("coding") or [{}])[0]
            for question in cls.ASK_AT_ORDER_ENTRY_QUESTIONS:
                q_code = question["code"]
                if q_code["system"] != obs_coding.get("system") or q_code["code"] != obs_coding.get("code"):
                    continue
                matched_observation_urls.add(obs_ref_url)
                if question["value_type"] == "codeable_concept":
                    value_coding = ((obs.get("valueCodeableConcept") or {}).get("coding") or [{}])[0]
                    if value_coding.get("code"):
                        aoe_values[question["link_id"]] = value_coding["code"]
                elif question["value_type"] == "date_time":
                    value = obs.get("valueDateTime")
                    if value:
                        aoe_values[question["link_id"]] = value[:10]
                elif question["value_type"] == "quantity":
                    value = (obs.get("valueQuantity") or {}).get("value")
                    if value is not None:
                        aoe_values[question["link_id"]] = str(value)
                break
        extracted["aoe_values"] = aoe_values

        # Observations that aren't one of the fixed
        # ASK_AT_ORDER_ENTRY_QUESTIONS (and so have nowhere to go as an
        # editable field) still get surfaced — read-only — rather than
        # silently dropped. Scans every Observation in the bundle, not
        # just ones service_request.supportingInfo actually references:
        # a real producer (examples/Liverpool_O21_Apr26.json) includes
        # over a dozen free-text Q&A-style Observations that
        # supportingInfo never references at all.
        extra_observations = []
        for full_url, resource in resources_by_ref.items():
            if resource.get("resourceType") != "Observation" or full_url in matched_observation_urls:
                continue
            label, value = cls._observation_label_and_value(resource)
            if label != "—" or value != "—":
                extra_observations.append({"label": label, "value": value})
        extracted["extra_observations"] = extra_observations

        return extracted

    @staticmethod
    def _observation_label_and_value(observation):
        """Best-effort (label, value) for an arbitrary Observation, for
        parse_order_message_bundle()'s "extra observations" (ones that
        aren't one of ASK_AT_ORDER_ENTRY_QUESTIONS, shown read-only
        instead). Handles two shapes:

        - The normal one: `code` names the question, a `value[x]`
          carries the answer (valueCodeableConcept's coding display/
          text, valueQuantity's value+unit, or the raw value for
          valueString/valueDateTime/valueInteger/valueBoolean).
        - A real producer's non-conformant one (Liverpool's O21 export,
          examples/Liverpool_O21_Apr26.json): no `value[x]` at all —
          the question text is in `code.coding[0].code` and the answer
          in `code.coding[0].display` instead. Only used as a fallback
          when there's genuinely no `value[x]` to prefer, so a properly
          value[x]-shaped Observation is never misread this way.
        """
        code = observation.get("code") or {}
        codings = code.get("coding") or []
        first = codings[0] if codings else {}

        value_text = None
        for key in ("valueCodeableConcept", "valueQuantity", "valueString", "valueDateTime", "valueInteger", "valueBoolean"):
            value = observation.get(key)
            if value is None:
                continue
            if key == "valueCodeableConcept":
                value_codings = value.get("coding") or []
                value_text = (value_codings[0].get("display") if value_codings else None) or value.get("text")
            elif key == "valueQuantity":
                value_text = " ".join(str(p) for p in [value.get("value"), value.get("unit") or value.get("code")] if p)
            else:
                value_text = str(value)
            break

        if value_text:
            label = code.get("text") or first.get("display") or first.get("code") or "—"
            return label, value_text

        if first.get("code") and first.get("display"):
            return first["code"].rstrip(":").strip(), first["display"]

        return (code.get("text") or first.get("display") or first.get("code") or "—"), "—"

    @staticmethod
    def extract_hl7v2_message(esb_response):
        """The HL7 v2 message embedded in a $process-message ESB
        response (see examples/OrderResponse.json) — this deployment's
        ESB replies with a message Bundle whose
        `OperationOutcome.issue[].diagnostics` carries the HL7 v2 the
        FHIR order was actually converted to and sent onward as
        (segments separated by "\\r", standard HL7 v2 — not a documented
        FHIR convention, just what this ESB happens to do). Returns None
        if `esb_response` isn't a Bundle, or has no OperationOutcome
        with a non-empty `diagnostics` string on any of its issues."""
        if not isinstance(esb_response, dict):
            return None
        for entry in esb_response.get("entry", []):
            resource = entry.get("resource") or {}
            if resource.get("resourceType") != "OperationOutcome":
                continue
            for issue in resource.get("issue", []):
                if issue.get("diagnostics"):
                    return issue["diagnostics"]
        return None
