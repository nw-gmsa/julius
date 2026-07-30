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
        # Basic auth, as configured for this server.
        self.user = user or os.environ.get("FHIR_USER", "sqluser")
        self.password = password or os.environ.get("FHIR_PASSWORD", "demo123")
        if verify_ssl is None:
            # Internal IP + likely self-signed cert -> default to NOT verifying.
            # Override with FHIR_VERIFY_SSL=true if your server has a real cert.
            verify_ssl = os.environ.get("FHIR_VERIFY_SSL", "false").lower() == "true"
        self.verify_ssl = verify_ssl
        self._ref_cache = {}  # reference string -> resolved resource (or None); process-lifetime only

    def _auth(self):
        return HTTPBasicAuth(self.user, self.password) if self.user else None

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

    def active_filler_orders(self):
        """
        All active genomic test orders (ServiceRequest) with
        `intent=filler-order` — i.e. orders as seen from the filler/lab
        system's side, as opposed to a placer/requesting-system order —
        system-wide, for the work order screen. Not scoped to one patient,
        so each order's specimen/patient/requester come back in the same
        query via `_include` (same shape as ctdna_orders()), and results
        paginate up to `_search_all_split`'s default cap (1,000 records) —
        see README for what to do if that's ever hit.

        Like ctdna_orders(), orders are identified by `resourceType` across
        `matches + included` combined rather than by trusting
        `Bundle.entry.search.mode` (see that method's docstring for the
        real bug this pattern fixes on servers that don't reliably tag it).
        """
        base_params = {
            "intent": "filler-order",
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
