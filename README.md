# FHIR Lab Explorer

A minimal Flask app for browsing genomic test orders (`ServiceRequest`)
and reports (`DiagnosticReport` + `Observation`) on a FHIR R4 server
conforming to the **NHS North West Genomics IG**
(https://nw-gmsa.github.io/en/), e.g. your HealthConnect CDR at
`https://192.168.1.62/healthconnect/cdr/fhir/r4/`.

Note this IG is specifically about **genomic** testing (rare disease,
cancer genomics, etc.) — not general chemistry/haematology labs — so
"lab order/report" here means genomic test order/report throughout.

## Run it

```bash
cd fhir-lab-explorer
python3 -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt

export FHIR_BASE_URL="https://192.168.1.62/healthconnect/cdr/fhir/r4"
export FHIR_USER="sqluser"
export FHIR_PASSWORD="demo123"
# If the server has a real (non-self-signed) TLS cert, turn verification on:
# export FHIR_VERIFY_SSL=true

python3 app.py
```

**Note on first install**: `requirements.txt` includes `scispacy`
and its `en_core_sci_sm` model (~150MB), needed for the clinical-term
extraction on the report/variants page. This makes the first
`pip install` noticeably slower than before — that's expected. If you'd
rather skip it for now, remove the last two lines from
`requirements.txt`; the app still works fine without it, the
clinical-terms table just shows a setup message instead of results.

**Note on first use of the clinical-terms page**: on top of the model
above, the UMLS entity-linking step downloads scispaCy's UMLS knowledge
base + nearest-neighbour index (~1GB) the first time `/report/<id>/variants`
is opened — expect that first request to take noticeably longer while it
downloads and caches under `~/.scispacy`. Subsequent requests (even after
restarting the app) reuse the cached files.

Then open **http://localhost:5050**. Search by patient name, NHS
number, or FHIR patient ID, then click through to see that patient's
genomic test orders and reports. Use the **Daily stats** link for a
day-by-day breakdown of orders/reports across all patients, the
**ctDNA summary** link for a cross-patient list of ctDNA order/result
turnaround, or the **Work orders** link for a cross-patient worklist of
active filler-order test orders.

## What's actually happening

- `fhir_client.py` — all the HTTP calls, using HTTP **Basic auth**
  (kept dependency-free apart from `requests`, so you can see exactly
  what's hitting the wire).
- `app.py` — Flask routes + a few Jinja filters to turn FHIR's nested
  JSON (CodeableConcept, valueQuantity, etc.) into readable text.
- Category codes come **straight from the IG's published profiles**,
  not guesses:
  - `ServiceRequest.category:GenomicProcedure` -> SNOMED `116148004`
    (https://nw-gmsa.github.io/en/StructureDefinition-ServiceRequest.html)
  - `DiagnosticReport.category:Genetics` -> HL7 v2-0074 code `GE`
    (https://nw-gmsa.github.io/en/StructureDefinition-DiagnosticReport.html)
  - Both queries **fall back automatically** to an unfiltered query
    if the categorized one returns nothing, in case this particular
    server instance doesn't populate category consistently.
- Patients are matched via **NHS number** (`identifier` search
  param) where possible, since the IG's `Patient` resources carry an
  `NHSIdentifier` — more reliable than name matching. Name search is
  still available as a fallback.
- TLS verification is **off by default** since `.62` is a private IP
  almost certainly running a self-signed cert. Flip `FHIR_VERIFY_SSL=true`
  if that's not the case.
- **Daily stats** (`/stats`) queries `ServiceRequest`/`DiagnosticReport`
  system-wide for a date range (default: last 7 days) using the
  `authored`/`date` search params, and breaks the results down by day,
  requesting/performing organisation, and genomic disease/indication.
  Indication for orders comes from `ServiceRequest.reasonCode`
  (Genomic Clinical Indication Codes). Reports don't carry their own
  indication, so their row follows `DiagnosticReport.basedOn` back to
  the originating order and reuses its `reasonCode`, falling back to
  the report's own `conclusionCode` if that link can't be resolved.
  Organisation/practitioner lookups are cached in-process for the
  life of the server, since the same few organisations show up
  repeatedly across a day's worth of orders. On top of the day/org/
  indication/ICS/country range totals, each of "by organisation" and
  "by indication" also gets a **day &times; that field pivot table**
  (`pivot_by_day()` in `app.py`), so trends over time are visible rather
  than just a range-wide count. To keep those tables from blowing out
  sideways when a field has many distinct values, only the top 10 values
  (by total count) get their own column — everything else is folded into
  an "Other" column.
- **ctDNA summary** (`/ctdna`) is a cross-patient turnaround-time view: order
  date, sample collection date, sample received date, date reported, and
  conclusion code, for genomic test orders that look like **ctDNA**
  (circulating tumour DNA) tests. This IG has no single confirmed Genomic
  Test Directory / SNOMED code specifically for ctDNA, so `_is_ctdna_order()`
  in `fhir_client.py` matches on the order's code text ("ctDNA", "circulating
  tumour/tumor DNA", "cfDNA", etc.) instead of an exact code — swap this for
  an exact code check if your server's ctDNA tests use a consistent one. The
  initial view shows **all outstanding orders** (any `ServiceRequest.status`
  other than `completed`) regardless of age, plus **orders completed in the
  last 30 days** (bounded by the linked report's `issued` date, or the order
  date if no report resolved) — there's no date-range picker on this screen
  yet, unlike `/stats`. `ctdna_orders()` queries `ServiceRequest` system-wide
  with no date bound (paginating like `/stats` does, same 1,000-record cap)
  and pulls in each order's specimen/patient/requester plus any linked
  `DiagnosticReport` via `_include`/`_revinclude` in the same query. Since
  some servers don't reliably tag `Bundle.entry.search.mode` on
  `_include`/`_revinclude`'d entries (which would otherwise misfile a
  linked report as if it were an order — the fix for a real bug reported
  against this screen), orders and linked reports are identified by
  `resourceType` across the whole result set rather than by trusting that
  tag. Rows are
  **split by managing organisation**, resolved from
  `ServiceRequest.requester` via the same reference chain as the stats
  screen's org breakdown (`order_organisation_resource()`): either the
  requester *is* an Organization directly, or it's a PractitionerRole whose
  `.organization` points at one. A requester that can't be resolved either
  way groups under "Unknown". The organisation heading also shows its NHS
  **ODS code** (`organisation_ods_code()`), read from the Organization
  resource's `identifier` where `system` is
  `https://fhir.nhs.uk/Id/ods-organization-code` — not confirmed against a
  real server, so if it never shows up, check what system value your
  server's Organization identifiers actually use. The **Test code** column
  shows the raw `ServiceRequest.code.coding[].code` value (not `.text`/
  `.display`) — deliberately, so it lines up with the Genomic Test
  Directory code list rather than a free-text label. The **iGene report
  ID** column (`igene_report_identifier()`) shows the identifier under the
  NW Genomics IG-specific system
  `https://fhir.nwgenomics.nhs.uk/iGene/ReportIdentifier`, checked on the
  `ServiceRequest` first and falling back to the linked `DiagnosticReport`,
  since it's not confirmed which resource actually carries it on a given
  server.
- **Work orders** (`/work-orders`) is a cross-patient worklist of active
  test orders — `ServiceRequest` with `intent=filler-order` and
  `status=active`, system-wide (`active_filler_orders()`), i.e. orders as
  seen from the filler/lab system's side rather than the requesting
  system's. Same table layout and `basedOn` chain rendering
  (`build_order_chains()`) as a patient page's "Genomic test orders" table,
  plus a Patient column since it spans multiple patients. First version —
  no filtering/date range yet, and no splitting by organisation the way
  `/ctdna` does.
- **Test orders** (`/test-orders`) is the placer-side counterpart to Work
  orders — same screen, same `active_filler_orders()`/`active_placer_orders()`
  query shape (both built on a shared `_active_orders_with_intent()`), but
  filters `ServiceRequest.intent` to "order" or "original-order" instead of
  "filler-order". The two intent values are joined into one comma-separated
  search param (`intent=order,original-order`) for FHIR's OR-within-a-param
  semantics — a repeated `intent=` parameter name would mean AND instead,
  which no single order's one `intent` value could ever satisfy. Two extra
  things on this screen only (not Work orders):
  - A **"Real NHS number" badge** next to any patient whose NHS number
    falls in the same 400,000,000–499,999,999 / 600,000,000–799,999,999
    ranges the admin screen uses (`FhirClient.nhs_number_in_ranges()`,
    extracted from `patients_in_nhs_number_ranges()` for reuse per-patient).
  - A **"Delete orders with unknown patient"** button (`/test-orders/
    clear-down-unknown-patient`, **destructive, irreversible**) — deletes
    every currently-active placer-order `ServiceRequest` whose patient
    can't be resolved (`orders_with_unknown_patient()`/
    `clear_down_orders_with_unknown_patient()`), broader than the admin
    screen's orphaned-`ServiceRequest` check since it also catches a
    `subject` reference that's present but dangling, not just a wholly
    absent one. Single `POST`, no separate confirm route — same reasoning
    as the admin screen's orphaned clear-down: no patient identity is
    involved, and the "Unknown" patient cells are already visible on the
    same page before the button is reachable.
- **Clear down patient data** (`/patient/<id>/clear-down`) — a **destructive,
  irreversible** button on the patient page that deletes every Specimen,
  DiagnosticReport, and ServiceRequest for that patient from the FHIR server
  (`clear_down_patient()`). Meant for resetting a test/demo patient between
  runs, not for real clinical records. `GET` shows a confirmation page
  listing exactly what will be deleted (counts + a table of each
  order/report/specimen); only `POST` (the confirm button's form) actually
  deletes anything, so a plain link/crawler/back-button can't trigger it by
  accident. The confirm form also has an **unticked-by-default checkbox** to
  additionally delete the **Patient resource itself** — ticking it switches
  to `clear_down_patient_and_record()` instead, the same method the admin
  screen's bulk delete uses. Reports and orders are deleted before specimens
  (in case a server enforces referential integrity — unverified either
  way). Continues past individual failures and reports a `{"deleted": [...],
  "failed": [...]}` breakdown rather than stopping at the first one, since a
  partial clear-down is still useful to see. **This route has no
  authentication or CSRF protection** (matching the rest of this app, which
  has none either), so don't expose this app beyond a trusted network/test
  environment if this button is enabled.
- **Admin screen** (`/admin`) — another **destructive, irreversible** area,
  this time bulk/system-wide rather than per-patient. Not linked from the
  nav bar (deliberately kept off the main navigation, but reachable directly
  at `/admin` — there's no auth gate behind it, so this is obscurity, not
  real access control):
  - **Test patients by NHS number range**: `patients_in_nhs_number_ranges()`
    fetches every Patient system-wide (FHIR identifier search can't do
    numeric ranges, so this filters client-side) and flags anyone whose NHS
    number falls in the conventional synthetic/test ranges
    400,000,000–499,999,999 or 600,000,000–799,999,999
    (`FhirClient.NHS_NUMBER_TEST_RANGES`). You tick which ones to remove;
    a confirm step re-resolves each selected patient's order/report/
    specimen counts before the final delete, which — unlike the per-patient
    clear-down button above — also deletes **the Patient resource itself**
    (`clear_down_patient_and_record()`), since fully purging synthetic test
    patients is the point here.
  - **Orphaned ServiceRequests**: `orphaned_service_requests()` finds every
    `ServiceRequest` with no `subject` reference at all (tries the
    `subject:missing=true` search modifier first, falling back to fetching
    everything and filtering client-side if that modifier isn't supported —
    unverified against this server). No per-row selection — it's an
    all-or-nothing "delete every orphan found" action
    (`clear_down_orphaned_service_requests()`), with the full list already
    shown on the same page as the delete button (no separate confirm step,
    since nothing there identifies a specific patient the way the patient
    clear-down does).
  - Same safety pattern as the per-patient clear-down: `GET` only
    searches/lists, `POST` is the only thing that deletes. **No auth/CSRF
    protection** here either — same caveat as above, more so given the
    blast radius (multiple patients, or every orphaned order, in one go).
- **Report PDFs & variant extraction** — `presentedForm.url` points at a
  **FHIR Binary resource** (e.g. `Binary/abc123`), not a static file.
  `fetch_attachment_bytes()` requests it as `application/fhir+json` (which
  reliably returns the Binary resource — a JSON object with `contentType`
  and base64 `data`) and decodes that, falling back to using raw bytes
  directly if a server ignores the Accept header. The "🧬 Extract variant
  types" link on each report runs that PDF's text layer through a
  **keyword scan** (`extract_variant_types()`, via `pdfplumber`) for known
  variant-type terms (missense, frameshift, splice site, CNV, etc.) and
  shows mention counts. This is plain keyword-spotting, not HGVS/VCF
  parsing — it can't tell a reported variant from an incidental mention
  (e.g. in a methods section), and finds nothing on scanned/image-only
  PDFs (no text layer to search). Treat it as a rough signal.
- **Clinical term extraction (scispaCy + UMLS linking)** — alongside the
  keyword scan, each report's PDF text also runs through scispaCy's
  `en_core_sci_sm` model for biomedical named-entity recognition. Each
  entity is then resolved to its best-matching UMLS concept via scispaCy's
  `EntityLinker` (CUI + canonical name + semantic type), and the
  clinical-terms table is grouped by that semantic type (Disease or
  Syndrome, Gene or Genome, Laboratory Procedure, etc.) instead of one flat
  list — entities that don't link confidently show up under "Unlinked".
  Both extractions share a single `extract_pdf_text()` call rather than
  re-parsing the PDF twice. The NER model is a ~150MB one-time-per-process
  download; the UMLS knowledge base + nearest-neighbour index the linker
  uses is separate and much bigger (~1GB), downloaded lazily on first use
  and cached under `~/.scispacy` after that — see setup below. If scispaCy
  isn't installed, the page still shows the keyword-scan results with a
  clear setup message in place of the clinical-terms table, rather than
  failing the whole page.
- **Geography** — each order/report row also resolves its patient
  (`subject`) and derives:
  - **ICS** from `Patient.managingOrganization`'s name.
  - **Country** from the patient's NHS-number identifier, using the
    codes you gave me (`X24` = England, `W00` = Wales). You didn't
    specify exactly where those codes live within `identifier`
    (`system`? `assigner`? an extension?), so `_find_country_code()`
    searches the whole identifier entry recursively for either code
    rather than assuming one fixed path — **this is the one part of
    this feature I couldn't verify and would check first** against a
    real Patient record. If it comes back "Unknown" for everyone,
    print a sample `Patient.identifier` array and I can narrow the
    lookup to the exact field.

## Things worth double-checking against your server

1. **Auth** is Basic (`sqluser` / `demo123`) as you specified — if
   the server later moves to OAuth2/SMART (the IG's own API Security
   volume, https://nw-gmsa.github.io/en/api-security.html, describes
   SMART-on-FHIR + IHE IUA as the target state), swap `_auth()` in
   `fhir_client.py` for Bearer-token support.
2. **Genomic Test Directory codes** — `ServiceRequest.code` and
   `DiagnosticReport.code` are bound to England's National Genomic
   Test Directory value set, not LOINC/SNOMED lab codes you might be
   used to. The `code_text` filter shows whatever's in `.text` or the
   first coding's `.display`, so it should render reasonably even
   without knowing the exact code list.
3. If a request fails, the app shows the raw error inline on the
   page — check that first (401 = credentials, SSL error = cert,
   404 = wrong base path).
4. **Stats screen date search** — I used the standard FHIR search
   params (`ServiceRequest?authored=ge...&le...`,
   `DiagnosticReport?date=ge...&le...`), which are correct per spec,
   but I couldn't confirm this server indexes them. If `/stats` comes
   back empty for a range you know has data, that's the first thing
   to check — try the same query directly against the FHIR endpoint.
5. **Stats pagination cap** — `/stats` follows up to 10 pages of 100
   results (1,000 records) per resource type per query. Fine for a
   week at a time; a very high-volume day range could silently hit
   that cap. Raise `max_pages` in `_search_all()` if needed.
6. **Stats now resolves one Patient per order/report** (for ICS and
   country), on top of the organisation lookups from before. The
   reference cache helps when the same patients recur, but a date
   range with many distinct patients will be noticeably slower than
   the org-only version was.
7. **Binary content negotiation** — I request Binary resources as
   `application/fhir+json` and decode `.data`, which should work on
   any spec-compliant server, but I couldn't test it against yours.
   If "View PDF" 404s or comes back empty, check what a direct
   `GET Binary/<id>` with that Accept header actually returns.
8. **Variant extraction accuracy** — the keyword list in
   `VARIANT_TYPE_TERMS` is generic HGVS/genomics terminology, not
   tuned to this IG's actual report wording. Open a real report's
   "Extract variant types" result next to the PDF itself and see if
   the counts look right — the term list is the first thing to adjust
   if your reports phrase things differently.
9. **UMLS linker threshold/coverage** — `extract_clinical_terms()` only
   links an entity to a UMLS concept if the top candidate scores
   ≥ `linker_threshold` (default 0.85); below that, or with no candidate
   at all, it's shown under "Unlinked" rather than guessing. I did run
   this end-to-end against synthetic clinical text (not a real IG
   report) and got sensible results — e.g. "TP53 gene" → *Gene or
   Genome*, "breast cancer" → *Neoplastic Process* — but the threshold
   and how much ends up "Unlinked" is worth checking against real report
   wording, and adjusting if it's too strict/loose.
10. **ctDNA text matching** — `/ctdna` identifies ctDNA orders by checking
    `ServiceRequest.code`'s text for "ctDNA"/"circulating tumour DNA"/"cfDNA"
    etc. (`CTDNA_TEXT_MATCHES` in `fhir_client.py`), since I don't have a
    confirmed Genomic Test Directory code for it. If your server's ctDNA
    tests come back empty on this screen, check what `code.text`/
    `code.coding[].display` actually looks like for a real ctDNA order and
    either adjust the match list or switch to an exact code check.
11. **"Completed in the last 30 days" cutoff** on `/ctdna` is a rolling
    window from today, bounded by the linked report's `issued` date — not a
    calendar month and not configurable yet (no date-range picker like
    `/stats` has). Outstanding orders (anything not `status: completed`)
    are shown with no date bound at all, which could be slow or show a lot
    of very old orders if this server has long-lived active `ServiceRequest`s
    that were never marked completed.
12. **ODS code system URI** — `organisation_ods_code()` looks for
    `identifier.system == "https://fhir.nhs.uk/Id/ods-organization-code"` on
    the resolved Organization, falling back to the first identifier with no
    `system` at all. I haven't confirmed this is the system value this
    server's Organization resources actually use — if ODS codes never show
    up next to organisation names on `/ctdna`, print a sample
    `Organization.identifier` array and adjust.
13. **iGene report identifier location** — `igene_report_identifier()`
    checks the `ServiceRequest`'s `identifier` list first, then the linked
    `DiagnosticReport`'s, for one with system
    `https://fhir.nwgenomics.nhs.uk/iGene/ReportIdentifier`. I don't know
    which resource this server actually populates it on (or whether it's
    populated at all) — if the "iGene report ID" column is always empty,
    check a real order/report pair directly.

## What I'd extend first

1. **Pagination on patient pages** — right now they grab up to 50
   records per resource type and stop; for patients with long
   histories you'll want to follow the `Bundle.link[rel=next]` URL
   (the stats screen already does this via `_search_all`).
2. **Better variant extraction** — the current approach is a keyword
   scan; a real implementation would want structured variant data
   (many genomics reports include a `Genomics-Variant` FHIR profile
   alongside/instead of the PDF, or the PDF has a consistent findings
   table pdfplumber's `extract_tables()` could parse directly) rather
   than text-mining free-form report prose.
