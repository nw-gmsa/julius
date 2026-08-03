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
import base64
import requests
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

    def _auth(self):
        return HTTPBasicAuth(self.user, self.password) if self.user else None

    def verify_credentials(self):
        """Minimal authenticated request to confirm self.user/self.password
        are accepted by the FHIR server. Raises requests.HTTPError (e.g. a
        401/403) or requests.RequestException (connection/SSL failure) if
        not — callers (the login route) catch these to show an error rather
        than letting a bad login silently through."""
        self._get("Patient", params={"_count": 1})

    def _headers(self):
        return {"Accept": "application/fhir+json"}

    def _get(self, path, params=None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = requests.get(
            url, headers=self._headers(), params=params or {},
            auth=self._auth(), verify=self.verify_ssl, timeout=15,
        )
        resp.raise_for_status()
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
        resp.raise_for_status()
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
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

    def _search_all_split(self, resource_type, params, max_pages=10):
        """Like _search_all(), but keeps _include/_revinclude'd resources
        separate from primary matches (see _split_bundle), across however
        many pages are followed. Used for system-wide queries that aren't
        scoped to one patient and can span many results."""
        all_matches, all_included = [], []
        bundle = self._get(resource_type, params)
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
            resp.raise_for_status()
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
        [{"value", "assigner_name", "assigner_ods"}, ...] — a patient can
        carry more than one MRN, one per assigning organisation (e.g. a
        separate MRN at each trust that's treated them), so this returns a
        list rather than a single value like nhs_number()/
        igene_patient_identifier() above.

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

    def _active_orders_with_intent(self, intent):
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

    def active_filler_orders(self):
        """All active genomic test orders with `intent=filler-order` — i.e.
        orders as seen from the filler/lab system's side. Used by the work
        orders screen. See _active_orders_with_intent()."""
        return self._active_orders_with_intent("filler-order")

    def active_placer_orders(self):
        """All active genomic test orders with `intent` of "order" or
        "original-order" — i.e. orders as seen from the placer/requesting
        system's side, as opposed to active_filler_orders()'s filler-order
        orders. Used by the test orders screen. See
        _active_orders_with_intent()."""
        return self._active_orders_with_intent("order,original-order")

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

    #: Cepheid GeneXpert BCR-ABL1 quantitative monitoring test code. Unlike
    #: ctDNA (no confirmed code at all, so text-matched) or the Genomic Test
    #: Directory code (a confirmed system, so system-matched), this is a
    #: known exact code value but not a confirmed system, so
    #: _is_bcrabl_report() matches by `coding[].code` regardless of system.
    BCRABL_CODE = "BCRABL"

    @classmethod
    def _is_bcrabl_report(cls, report):
        codings = (report.get("code") or {}).get("coding", [])
        return any(c.get("code") == cls.BCRABL_CODE for c in codings)

    def bcrabl_reports(self, start_date=None, end_date=None):
        """
        All DiagnosticReport resources system-wide with a BCRABL code (see
        _is_bcrabl_report) — the Cepheid Test Results screen. Paginated
        like other system-wide queries in this file (see README for the
        pagination cap).

        Optionally bounded to DiagnosticReport.date within
        [start_date, end_date] (ISO dates, same convention as
        orders_in_range()/reports_in_range()) — pass neither to fall back
        to the old unbounded query.

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

    # ---- PDF text extraction, variant keywords, and clinical terms -----

    @staticmethod
    def extract_pdf_text(pdf_bytes):
        """Extract the text layer of a PDF via pdfplumber. Returns "" for
        scanned/image-only PDFs (no text layer to extract) rather than
        raising — callers should treat empty text as "nothing found",
        not as an error."""
        import pdfplumber
        import io

        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n".join(text_parts)

    #: Heuristic keyword list for spotting variant-type mentions in report
    #: text. This is a plain-text keyword scan, not HGVS/VCF parsing — it
    #: will miss anything phrased unusually and can't confirm a match is
    #: describing an actually-reported variant vs. incidental text (e.g. a
    #: methods section). Treat results as a rough signal, not ground truth.
    VARIANT_TYPE_TERMS = [
        "frameshift deletion", "frameshift insertion", "frameshift duplication", "frameshift variant",
        "in-frame deletion", "in-frame insertion", "in-frame duplication",
        "splice donor variant", "splice acceptor variant", "splice site variant", "splice region variant",
        "missense variant", "nonsense variant", "synonymous variant",
        "stop gained", "stop lost", "start lost",
        "copy number gain", "copy number loss", "copy number variant",
        "structural variant", "single nucleotide variant",
        "deletion", "duplication", "insertion", "substitution",
        "translocation", "inversion", "indel", "snv", "cnv",
    ]

    @classmethod
    def extract_variant_types(cls, text):
        """
        Count mentions of known variant-type terms in already-extracted PDF
        text (see extract_pdf_text). Returns {term: count}, sorted by count
        desc, omitting terms with zero matches. More specific terms (e.g.
        "frameshift deletion") are matched independently of their generic
        substrings ("deletion"), so one phrase can add to more than one
        bucket — intentional for a rough-signal tool, but means counts
        aren't mutually exclusive.
        """
        import re

        text_lower = text.lower()
        counts = {}
        for term in cls.VARIANT_TYPE_TERMS:
            pattern = r"\b" + re.escape(term) + r"s?\b"
            n = len(re.findall(pattern, text_lower))
            if n:
                counts[term] = n
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    #: Lazily-loaded scispaCy pipeline, cached at module level so the model
    #: (several hundred MB, slow to load) is only loaded once per process,
    #: not once per request.
    _scispacy_nlp = None

    @classmethod
    def _get_scispacy_pipeline(cls):
        if cls._scispacy_nlp is None:
            import spacy
            import scispacy.linking  # noqa: F401 - registers the "scispacy_linker" spaCy factory

            # en_core_sci_sm is scispaCy's general-purpose biomedical NER
            # model — broad "clinical entity" spans, not typed as
            # disease/gene/chemical specifically. For typed extraction, swap
            # in en_ner_bc5cdr_md (disease/chemical) or en_ner_bionlp13cg_md
            # (genes/cell types) instead — see README.
            nlp = spacy.load("en_core_sci_sm")
            # UMLS EntityLinker: resolves each NER span to its best-matching
            # UMLS concept (CUI + canonical name + semantic type), so results
            # can be grouped by category (disease/gene/procedure/...) instead
            # of a flat list of raw text spans. First use downloads scispaCy's
            # UMLS knowledge base + approximate-nearest-neighbour index
            # (~1GB, separate from and larger than the NER model above) —
            # cached under ~/.scispacy after that.
            nlp.add_pipe("scispacy_linker", config={"linker_name": "umls"})
            cls._scispacy_nlp = nlp
        return cls._scispacy_nlp

    @classmethod
    def extract_clinical_terms(cls, text, limit=50, linker_threshold=0.85):
        """
        Run scispaCy NER + UMLS entity linking over already-extracted PDF
        text. Returns the most frequently mentioned clinical entities as a
        list of dicts — {"term", "count", "cui", "canonical_name",
        "category"} — sorted by count desc. Terms are deduplicated
        case-insensitively (keeping the first-seen casing for "term").

        Each entity is linked to its highest-scoring UMLS candidate (from
        `Span._.kb_ents`) if that score clears `linker_threshold`; below
        that, or with no candidates at all, the entity is left unlinked
        ("category": "Unlinked", cui/canonical_name None) rather than
        guessing. `category` is the linked concept's first semantic type
        (TUI), resolved to its official UMLS name via the semantic type
        tree that scispaCy's UMLS knowledge base loads automatically
        (`linker.kb.semantic_type_tree`) — falling back to the raw TUI code
        for the rare type missing from that tree.

        Raises ImportError if scispacy/spacy aren't installed, or OSError if
        the NER model or UMLS knowledge base isn't downloaded — callers
        should catch both and show a setup hint rather than a raw traceback.
        """
        if not text.strip():
            return []
        nlp = cls._get_scispacy_pipeline()
        linker = nlp.get_pipe("scispacy_linker")
        type_tree = linker.kb.semantic_type_tree
        doc = nlp(text)

        counts, display, link = {}, {}, {}
        for ent in doc.ents:
            term = ent.text.strip()
            if len(term) < 2:
                continue
            key = term.lower()
            counts[key] = counts.get(key, 0) + 1
            display.setdefault(key, term)
            if key in link:
                continue

            candidates = [c for c in ent._.kb_ents if c[1] >= linker_threshold]
            if candidates:
                cui, _score = max(candidates, key=lambda c: c[1])
                entity = linker.kb.cui_to_entity[cui]
                tui = entity.types[0] if entity.types else None
                category = "Unlinked"
                if tui and tui in type_tree.type_id_to_node:
                    category = type_tree.get_canonical_name(tui)
                elif tui:
                    category = tui
                link[key] = {"cui": cui, "canonical_name": entity.canonical_name, "category": category}
            else:
                link[key] = {"cui": None, "canonical_name": None, "category": "Unlinked"}

        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return [{"term": display[key], "count": count, **link[key]} for key, count in ranked]

    # ---- Stats: system-wide date-range queries -------------------------

    def orders_in_range(self, start_date, end_date):
        """All genomic test orders authored within [start_date, end_date]
        (ISO dates), across all patients — used by the daily stats screen."""
        params = {
            "authored": [f"ge{start_date}", f"le{end_date}"],
            "category": SERVICE_REQUEST_CATEGORY,
            "_count": 100,
        }
        try:
            entries = self._search_all("ServiceRequest", params)
            if entries:
                return entries
        except requests.HTTPError:
            pass
        return self._search_all("ServiceRequest", {
            "authored": [f"ge{start_date}", f"le{end_date}"], "_count": 100,
        })

    def reports_in_range(self, start_date, end_date):
        """All genomic test reports dated within [start_date, end_date]
        (ISO dates), across all patients — used by the daily stats screen."""
        params = {
            "date": [f"ge{start_date}", f"le{end_date}"],
            "category": DIAGNOSTIC_REPORT_CATEGORY,
            "_count": 100,
        }
        try:
            entries = self._search_all("DiagnosticReport", params)
            if entries:
                return entries
        except requests.HTTPError:
            pass
        return self._search_all("DiagnosticReport", {
            "date": [f"ge{start_date}", f"le{end_date}"], "_count": 100,
        })

    # ---- ctDNA summary: system-wide, no date bound ---------------------

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

    @classmethod
    def _is_ctdna_order(cls, order):
        text = (cls._code_text(order.get("code")) or "").lower()
        return any(term in text for term in cls.CTDNA_TEXT_MATCHES)

    def ctdna_orders(self):
        """
        All genomic test orders (ServiceRequest) that look like ctDNA tests
        (see _is_ctdna_order), system-wide, with no date bound — the ctDNA
        summary screen needs every outstanding order regardless of age, not
        just a recent window (unlike orders_in_range, used by /stats).

        Each order's specimen, patient, and requester (for managing-
        organisation grouping via order_organisation()) come back in the
        same query via `_include`, and any DiagnosticReport whose `basedOn`
        points at one of these orders comes back via
        `_revinclude=DiagnosticReport:based-on` (plus that report's own
        specimen via `_include:iterate`, in case a server attaches the
        specimen to the report rather than the order, and the Practitioner/
        Organization behind a PractitionerRole requester, one hop further).
        Paginates up to `_search_all_split`'s default cap (1,000 records) —
        see README for what to do if this ever hits that.

        Returns (orders, reports_by_order_id): `orders` is the filtered
        ctDNA ServiceRequest list; `reports_by_order_id` maps a
        ServiceRequest id to its most-recently-issued linked
        DiagnosticReport (there should usually be at most one, but this
        picks the latest if a reflex/repeat test produced more).

        Both are built by filtering on `resourceType` across every resource
        the search returned (matches + included) rather than trusting
        Bundle.entry.search.mode to have sorted "match" (ServiceRequest)
        from "include" (everything else) correctly — some servers don't
        reliably set search.mode on _include/_revinclude'd entries, which
        would otherwise misfile a linked DiagnosticReport as if it were an
        order (with none of the ServiceRequest's own fields) and leave
        reports_by_order_id empty.
        """
        base_params = {
            "_count": 100,
            "_include": ["ServiceRequest:specimen", "ServiceRequest:patient", "ServiceRequest:requester"],
            "_revinclude": "DiagnosticReport:based-on",
            "_include:iterate": SERVICE_REQUEST_ITERATE_INCLUDES + ["DiagnosticReport:specimen"],
        }
        try:
            matches, included = self._search_all_split(
                "ServiceRequest", {**base_params, "category": SERVICE_REQUEST_CATEGORY})
        except requests.HTTPError:
            matches, included = [], []
        if not matches:
            matches, included = self._search_all_split("ServiceRequest", base_params)
        self._cache_included(matches + included)

        # Some servers don't reliably set Bundle.entry.search.mode on
        # _include/_revinclude'd entries (it's an easy detail to miss when
        # hand-rolling _revinclude support) — when that happens, _split_bundle
        # defaults those entries to "match", so a linked DiagnosticReport can
        # end up in `matches` instead of `included` (and get misread as if it
        # were a ServiceRequest "order", with none of its fields). Pool both
        # lists and filter by resourceType instead of trusting search.mode to
        # have sorted them correctly.
        all_resources = matches + included
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
    def _identifier_by_type(resource, type_code):
        """Identifier value from `resource.identifier` whose `.type.coding`
        includes the given v2-0203 code (see IDENTIFIER_TYPE_SYSTEM/
        PLACER_IDENTIFIER_TYPE/FILLER_IDENTIFIER_TYPE) — the standard FHIR
        way of distinguishing a placer order number from a filler order
        number on the same ServiceRequest."""
        if not resource:
            return None
        for ident in resource.get("identifier", []):
            codings = (ident.get("type") or {}).get("coding", [])
            if any(c.get("code") == type_code for c in codings):
                return ident.get("value")
        return None

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

    # ---- Patient data clear-down (destructive) -------------------------

    def clear_down_patient(self, patient_id):
        """
        DELETE every Specimen, DiagnosticReport, and ServiceRequest
        resource for a patient from the FHIR server — irreversible. Meant
        for resetting a test/demo patient's genomic test data between runs,
        not for use against real clinical records. Patient and Observation
        resources are left alone — only the three resource types the
        clear-down button is documented as deleting are touched.

        Deletes reports and orders before specimens, on the theory that a
        server enforcing referential integrity is more likely to reject
        deleting a Specimen still referenced by a live DiagnosticReport/
        ServiceRequest than the reverse — FHIR doesn't mandate this
        ordering though, so a real server's behaviour here is unverified.

        Returns {"deleted": [...], "failed": [...]}, each a list of
        "ResourceType/id" strings, so the caller can show exactly what
        happened rather than a single pass/fail flag — a partial
        clear-down (e.g. one order the server refuses to delete) is still
        useful information, not a reason to hide everything else that did
        get deleted.
        """
        orders = self.lab_orders_for_patient(patient_id)
        reports = self.lab_reports_for_patient(patient_id)

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
        fetches every Patient system-wide (paginated via _search_all, same
        1,000-record cap as other system-wide queries in this file) and
        filters client-side.
        """
        patients = self._search_all("Patient", {"_count": 100})
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

    def requester_display(self, order):
        """
        Resolve ServiceRequest.requester (PractitionerRole | Organization per
        the IG) into a human-readable "Dr X (Org Y)" style string.
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

        if rtype == "PractitionerRole":
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

            if practitioner_name and org_name:
                return f"{practitioner_name} ({org_name})"
            return practitioner_name or org_name or requester_ref.get("display") or "—"

        # Unexpected resource type — show what we can.
        return requester_ref.get("display") or resource.get("id", "—")

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
