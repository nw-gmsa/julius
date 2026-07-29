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

    def _search_all(self, resource_type, params, max_pages=10):
        """Search + follow Bundle.link[rel=next] pages, up to max_pages.
        Used for stats queries which aren't scoped to one patient and can
        span many results."""
        results = []
        bundle = self._get(resource_type, params)
        results.extend(self._entries(bundle))
        pages = 1
        while pages < max_pages:
            next_url = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
            if not next_url:
                break
            resp = requests.get(next_url, headers=self._headers(), auth=self._auth(),
                                 verify=self.verify_ssl, timeout=15)
            resp.raise_for_status()
            bundle = resp.json()
            results.extend(self._entries(bundle))
            pages += 1
        return results

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
