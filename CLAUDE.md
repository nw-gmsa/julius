# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A minimal Flask app for browsing genomic test orders (`ServiceRequest`) and
reports (`DiagnosticReport` + `Observation`) on a FHIR R4 server conforming to
the **NHS North West Genomics IG** (https://nw-gmsa.github.io/en/). This IG is
specifically about **genomic** testing (rare disease, cancer genomics) — not
general chemistry/haematology labs — so "lab order/report" throughout the code
means genomic test order/report.

## Running it

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export FHIR_BASE_URL="https://192.168.1.62/healthconnect/cdr/fhir/r4"
export FHIR_USER="sqluser"
export FHIR_PASSWORD="demo123"
# If the server has a real (non-self-signed) TLS cert, turn verification on:
# export FHIR_VERIFY_SSL=true

python3 app.py   # serves on http://localhost:5050 (override with PORT env var)
```

There is no test suite, linter, or build step in this repo — it's a two-file
Flask app plus Jinja templates.

`requirements.txt` includes `scispacy` and its `en_core_sci_sm` model
(~150MB download), needed only for clinical-term extraction on the
report/variants page. If it's not installed, the app still works — the
clinical-terms table just shows a setup message instead of results.

## Architecture

Two Python modules, both required reading before touching either:

- **`fhir_client.py`** — all HTTP calls to the FHIR server, using HTTP Basic
  auth. Deliberately dependency-free (just `requests`) so the raw REST calls
  stay visible rather than hidden behind a FHIR client library. Also owns PDF
  text extraction and keyword/NER-based analysis of report attachments.
- **`app.py`** — Flask routes only. Composes `FhirClient` calls and passes
  results to templates; also defines the Jinja filters (`human_name`,
  `code_text`, `obs_value`, `specimen_collected`, `specimen_received`) used to
  render FHIR's nested JSON (CodeableConcept, valueQuantity, etc.) as text.

### Category codes come from the IG, not guesses

- `ServiceRequest.category:GenomicProcedure` → SNOMED `116148004`
- `DiagnosticReport.category:Genetics` → HL7 v2-0074 code `GE`

Both queries **fall back automatically** to an unfiltered query if the
categorized search returns nothing, in case a given server doesn't populate
category consistently (see `lab_orders_for_patient` / `lab_reports_for_patient`
/ `orders_in_range` / `reports_in_range` in `fhir_client.py` — the
try-categorized-then-fall-back pattern repeats across all four).

### Patient matching

Patients are matched via **NHS number** (`identifier` search param) first,
since the IG's `Patient` resources carry an `NHSIdentifier` — more reliable
than name matching, which is available as a fallback.

### Order chains (basedOn)

`FhirClient.build_order_chains()` turns a patient's flat `ServiceRequest` list
into a parent/child tree via `basedOn` (the IG's link from a
reanalysis/cascade-testing request back to its originating order), returning
root nodes shaped `{"order": ..., "children": [...]}`. A parent is only
linked if it's present in the same list — a missing parent (paginated away)
or a would-be cycle (bad/circular `basedOn` data) falls back to treating the
order as a root rather than dropping it or looping forever. `patient.html`
renders this recursively via a self-calling Jinja macro
(`render_order_chain`), indenting children under their parent with `↳`.

### Reference resolution

`resolve_reference()` fetches whatever resource a FHIR `Reference` points to
and caches results in `FhirClient._ref_cache` for the life of the process.
Requester display, organisation lookups, and geography (below) all funnel
through this cache since the same organisations/practitioners recur across a
day's data.

`lab_orders_for_patient()` / `lab_reports_for_patient()` warm this cache up
front via `_include`/`_include:iterate` (`SERVICE_REQUEST_INCLUDES`,
`SERVICE_REQUEST_ITERATE_INCLUDES`, `DIAGNOSTIC_REPORT_INCLUDES` in
`fhir_client.py`), bundling each order/report's requester, specimens, and
(for reports) result Observations into the same search Bundle. `_split_bundle()`
separates primary matches from `_include`d resources using
`Bundle.entry.search.mode`, and `_cache_included()` seeds `_ref_cache` with
the latter — so `requester_display()`, `resolve_specimens()`, and
`observations_for_report()` (which now goes through `resolve_reference()`
rather than fetching directly) serve from cache instead of firing one GET
per resource. Falls back gracefully to per-resource GETs if a server doesn't
support `_include` (untested: whether this server supports the `:iterate`
modifier used to reach the Practitioner/Organization behind a
PractitionerRole requester).

### Daily stats (`/stats`)

Queries `ServiceRequest`/`DiagnosticReport` system-wide for a date range
(default: last 7 days) via `authored`/`date` search params, using
`_search_all()` to follow `Bundle.link[rel=next]` pages (capped at
`max_pages=10`, i.e. 1,000 records per resource type per query — raise
`max_pages` if a high-volume range silently hits the cap). Breaks results down
by day, organisation, indication, ICS, and country.

- Order indication comes from `ServiceRequest.reasonCode`.
- Reports don't carry their own indication: `report_indication()` follows
  `DiagnosticReport.basedOn` back to the originating order and reuses its
  `reasonCode`, falling back to the report's own `conclusionCode` if the link
  can't be resolved.
- Stats resolves one `Patient` per order/report for ICS/country — a date range
  with many distinct patients will be noticeably slower than an org-only
  aggregation would be, even with the reference cache.
- Beyond the range-wide `group_count()` breakdowns, `app.pivot_by_day()`
  builds a day-by-organisation and day-by-indication cross-tab for both
  orders and reports (`order_pivot_org`/`order_pivot_indication`/
  `report_pivot_org`/`report_pivot_indication`), so trends over the date
  range are visible rather than just totals. Each returns
  `{"days": [...], "columns": [...], "table": {day: {column: count}}}`;
  `columns` caps at the 10 most frequent values (by total count across the
  whole range), folding everything else into an "Other" column so a
  high-cardinality field (free-text indications especially) can't blow the
  table out sideways. `stats.html`'s `render_pivot()` macro renders any of
  the four from that same shape.

### ctDNA summary (`/ctdna`)

A cross-patient turnaround-time view for **ctDNA** (circulating tumour DNA)
genomic test orders: order date, sample collection date, sample received
date, date reported, and conclusion code, one row per order.

- **ctDNA detection is text-based, not code-based**:
  `FhirClient._is_ctdna_order()` checks `ServiceRequest.code`'s text against
  `CTDNA_TEXT_MATCHES` ("ctdna", "circulating tumour/tumor dna", "cfdna",
  etc.), since this IG has no single confirmed Genomic Test Directory/
  SNOMED code specifically for ctDNA. Swap for an exact `code.coding[].code`
  check if a server's ctDNA tests use a consistent one — see README.
- **`FhirClient.ctdna_orders()`** queries `ServiceRequest` system-wide with
  **no date bound** (via `_search_all_split()`, the split-aware sibling of
  `_search_all()` — both share pagination logic, capped at the same
  1,000-record default), bundling each order's `specimen`/`patient`/
  `requester` via `_include`, and any linked `DiagnosticReport` via
  `_revinclude=DiagnosticReport:based-on` (+ `_include:iterate` for that
  report's own specimen and the Organization/Practitioner behind a
  PractitionerRole requester — reuses `SERVICE_REQUEST_ITERATE_INCLUDES`,
  the same constant the patient-page queries use). Returns
  `(orders, reports_by_order_id)` — the latter maps an order's id to its
  most-recently-issued linked report, since a reflex/repeat test could
  produce more than one. **Both are built by filtering `resourceType`
  across `matches + included` combined, not by trusting
  `Bundle.entry.search.mode`** — some servers don't reliably tag
  `search.mode` on `_include`/`_revinclude`'d entries, which would
  otherwise misfile a linked `DiagnosticReport` into `matches` (getting
  read as if it were an order, with none of the ServiceRequest's fields —
  empty order date/specimen data) and leave `reports_by_order_id` unable
  to find it at all. This was a real bug, not just a theoretical one — fix
  it the same way if a similar split-then-filter pattern shows up
  elsewhere.
- **The outstanding/completed split happens in `app.ctdna_summary()`**, not
  in `fhir_client.py`: "outstanding" is any `ServiceRequest.status` other
  than `completed`, shown regardless of age; "completed" orders are only
  included if their linked report's `issued` date (or the order's
  `authoredOn` if no report resolved) falls within a rolling 30-day window
  from today. There's no date-range picker on this screen (unlike
  `/stats`) — the 30-day cutoff is currently fixed.
- Rows sort Outstanding-before-Completed, most-recently-ordered first
  within each group (two stable sorts on `rows`, applied in that order so
  both hold at once), **then split by managing organisation** via
  `app.group_rows_by_organisation()` (same alphabetical-with-"Unknown"-last
  pattern as `group_clinical_terms_by_category()`). The organisation
  resource itself comes from `FhirClient.order_organisation_resource()`,
  which resolves `ServiceRequest.requester` down to the Organization
  resource (not just its name) — either it *is* an Organization directly, or
  it's a PractitionerRole whose `.organization` points at one; unresolvable
  requesters fall back to `order_organisation()`'s display-text handling, or
  "Unknown". `organisation_ods_code()` then pulls the NHS ODS code from that
  resource's `identifier` (matching `system ==
  ODS_ORGANIZATION_CODE_SYSTEM`, falling back to any system-less
  identifier — unverified against a real server, see README), and
  `app.ctdna_summary()` appends it to the org name as `"Name (ODS)"` before
  grouping, so the group key and the displayed heading are the same string.
  `ctdna.html` loops over `rows_by_org`, rendering one `<h2>` + table per
  organisation.
- **Test code column** uses `app.code_value()`, not the `code_text` filter
  used everywhere else — it returns the first coding's raw `.code`
  (e.g. the Genomic Test Directory code), ignoring `.text`/`.display`
  entirely. This is the one column on this screen that deliberately shows a
  code instead of a human-readable label.
- **iGene report ID column** uses `FhirClient.igene_report_identifier(order,
  report)`, which checks `identifier.system ==
  IGENE_REPORT_IDENTIFIER_SYSTEM` (`https://fhir.nwgenomics.nhs.uk/iGene/
  ReportIdentifier`) on the order first, then the report — not confirmed
  which resource this server actually populates it on, so both are checked
  rather than assuming one.

### Work orders (`/work-orders`) & Test orders (`/test-orders`)

Two cross-patient worklists sharing everything except the `intent` filter:
Work orders shows active `ServiceRequest`s with `intent=filler-order`
(orders as seen from the filler/lab system's side); Test orders shows
`intent` of "order" or "original-order" (the placer/requesting system's
side). Both call `FhirClient._active_orders_with_intent(intent)` — a single
shared implementation — via the thin wrappers `active_filler_orders()` and
`active_placer_orders()`. The two intent values for Test orders are
comma-joined into one search param (`intent=order,original-order`) for
FHIR's OR-within-a-param semantics; a repeated `intent=` *parameter name*
means AND instead (as used elsewhere in this file for date ranges), which
no single order's one `intent` value could ever satisfy.

`app._order_worklist(fetch_orders)` is the shared route logic behind both
`/work-orders` and `/test-orders`: it calls `fetch_orders()` (one of the
two methods above), then builds the per-order requester/patient lookups
and the `build_order_chains()` tree — the two routes just plug in a
different fetch function and template.

Both screens deliberately reuse the patient page's "Genomic test orders"
table shape — same columns (Test/Status/Intent/Ordered/Requested
by/Placer ID/Filler ID/Reason Code Ref#/ID) and the same
`render_order_chain` macro for `basedOn` nesting — plus a Patient column
(`order_patient` dict, built the same way `order_requester` already is)
since these span multiple patients rather than being scoped to one.

`_active_orders_with_intent()` mirrors `ctdna_orders()`'s query shape:
`_include`s `specimen`/`patient`/`requester` (plus `_include:iterate` for
the Practitioner/Organization behind a PractitionerRole requester), and —
same lesson as the real bug fixed in `ctdna_orders()` — filters by
`resourceType` across `matches + included` combined rather than trusting
`Bundle.entry.search.mode`.

First version: no date-range picker, and no splitting by organisation the
way `/ctdna` does — flagged in README as the obvious next steps if either
needs to scale.

### Patient data clear-down (`/patient/<id>/clear-down`) — destructive

Deletes every Specimen, DiagnosticReport, and ServiceRequest for a patient
from the FHIR server. `FhirClient.clear_down_patient()` fetches the
patient's orders/reports/specimens (reusing `lab_orders_for_patient()`/
`lab_reports_for_patient()`/`resolve_specimens()`), then deletes reports
and orders before specimens via `FhirClient._delete()` (a thin
`requests.delete()` wrapper that treats a 404 as already-cleared and never
raises — it returns `False` on any failure so the caller can keep going
rather than aborting on the first rejected delete). Patient and Observation
resources are deliberately left alone.

**GET vs POST is the safety mechanism, not an afterthought**:
`app.patient_clear_down()` handles both methods on the same route — `GET`
only fetches and displays what *would* be deleted
(`patient_clear_down_confirm.html`), `POST` (the confirm button's form)
is the only path that calls `clear_down_patient()`
(`patient_clear_down_result.html`). This is the correct way to build any
delete control (a link/crawler/back-button can trigger a GET but never a
form POST), not something layered on afterward — don't "simplify" this
into a single bare link.

This route has **no auth, no CSRF token, and no rate limiting** — same as
every other route in this app, but worth calling out specifically here
since this one is destructive and irreversible rather than read-only. Only
run this app somewhere trusted if the clear-down feature is reachable.

### Admin screen (`/admin`) — bulk/system-wide, destructive

Two independent clear-down actions, both system-wide rather than scoped to
one patient:

- **Test patients by NHS number range** — `FhirClient.
  patients_in_nhs_number_ranges(ranges=None)` (default
  `NHS_NUMBER_TEST_RANGES`: 400,000,000–499,999,999 and
  600,000,000–799,999,999, the conventional synthetic/test NHS number
  ranges) fetches every `Patient` system-wide via `_search_all()` and
  filters client-side by parsing `nhs_number()`'s digits to an int — FHIR
  identifier search is exact-match only, no numeric range support.
  `app.admin()` (GET) lists matches with checkboxes; `POST
  /admin/patients/confirm` re-resolves each *selected* patient's
  order/report/specimen counts (not just an ID echo) so the final page
  shows real numbers before anything is deleted; `POST
  /admin/patients/clear-down` is the only route that calls
  `clear_down_patient_and_record()` — `clear_down_patient()` plus a
  `Patient/<id>` delete, since (unlike the per-patient button above)
  removing the Patient record itself is the whole point of this action.
- **Orphaned ServiceRequests** — `orphaned_service_requests()` tries
  `subject:missing=true` first, falling back to fetching every
  `ServiceRequest` system-wide and filtering on an absent `subject`
  client-side if that search modifier isn't supported (unverified against
  this server). Unlike the patient action, there's no per-row selection or
  separate confirm route — `admin()`'s GET already lists every orphan in
  full, and `POST /admin/orphaned/clear-down` deletes all of them
  (`clear_down_orphaned_service_requests()`) directly. The asymmetry is
  deliberate: this action doesn't touch anything patient-identifying, so
  the list-then-one-button flow already used for it is proportionate,
  whereas the patient action gets the extra confirm round-trip because it
  deletes identifiable individuals' records.

Both actions share `admin_clear_down_result.html` (parametrized by
`title`) for their result page, and both follow the same GET-lists/
POST-mutates split as the per-patient clear-down — `admin()`'s GET and
`admin_patients_confirm()`'s POST never call any `_delete()`-backed
method; only `admin_patients_clear_down()` and `admin_orphaned_clear_down()`
do. Same no-auth/no-CSRF caveat as above, more so given the larger blast
radius (multiple patients, or every orphaned order, per click).

### Report PDFs & variant/clinical-term extraction

`DiagnosticReport.presentedForm.url` points at a **FHIR Binary resource**
(e.g. `Binary/abc123`), not a static file. `fetch_attachment_bytes()` requests
it as `application/fhir+json` (reliably returns a JSON object with
`contentType` + base64 `data`) and decodes that, falling back to raw bytes if
a server ignores the Accept header.

Both extraction features share a single `extract_pdf_text()` call (via
`pdfplumber`) rather than re-parsing the PDF twice:

- **Variant keyword scan** (`extract_variant_types()`) — counts mentions of
  known variant-type terms (`VARIANT_TYPE_TERMS`: missense, frameshift, splice
  site, CNV, etc.). This is plain keyword-spotting, not HGVS/VCF parsing — it
  can't distinguish a reported variant from an incidental mention (e.g. in a
  methods section), and finds nothing on scanned/image-only PDFs. Treat it as
  a rough signal; the term list is the first thing to adjust if reports phrase
  things differently than expected.
- **Clinical term extraction + UMLS linking** (`extract_clinical_terms()`) —
  runs the PDF text through scispaCy's `en_core_sci_sm` model for biomedical
  NER, then resolves each entity to its best-matching UMLS concept via a
  `scispacy_linker` (`EntityLinker`) pipe, added alongside the NER model in
  `_get_scispacy_pipeline()` (both lazily loaded once per process into
  `FhirClient._scispacy_nlp`). A candidate is only accepted if its score
  clears `linker_threshold` (default 0.85); otherwise the entity is returned
  with `"category": "Unlinked"` rather than a guessed concept. Linked
  entities get their category from the *official* UMLS semantic type name —
  via `linker.kb.semantic_type_tree` (scispaCy's `UmlsKnowledgeBase` builds
  this automatically, no extra code needed), not a hand-maintained mapping.
  Returns `[{"term", "count", "cui", "canonical_name", "category"}, ...]`;
  `app.group_clinical_terms_by_category()` groups that into
  `[(category, [term, ...]), ...]` (alphabetical, "Unlinked" last) for
  `variants.html` to render as one sub-table per category. Raises
  `ImportError`/`OSError` if scispacy/the NER model/UMLS KB aren't
  installed/downloaded — `app.py` catches both and shows a setup message
  rather than failing the page. The UMLS KB + ANN index (~1GB, separate
  from the ~150MB NER model) downloads lazily on first use and is cached
  under `~/.scispacy` after that — end-to-end verified against synthetic
  clinical text in this environment (not yet against a real IG report).

### Geography (ICS / country)

Each stats row resolves its patient (`subject`) and derives:

- **ICS** from `Patient.managingOrganization`'s name.
- **Country** from the patient's NHS-number identifier, using codes `X24`
  (England) / `W00` (Wales). The exact field these codes live in
  (`system`? `assigner`? an extension?) was never confirmed against a real
  server, so `_find_country_code()` recursively searches the whole identifier
  entry for either code rather than assuming one fixed path. **If this comes
  back "Unknown" for everyone against a real server, print a sample
  `Patient.identifier` array and narrow the lookup to the exact field.**

## Things that are unverified against a real server

These are noted inline in the code/README as best-effort implementations that
haven't been exercised against a live NHS North West Genomics IG server:

1. **Auth is Basic** (`sqluser`/`demo123`). If the server moves to
   OAuth2/SMART (the IG's target state per its API Security volume), swap
   `_auth()` in `fhir_client.py` for Bearer-token support.
2. **Binary content negotiation** (`fetch_attachment_bytes`) — should work per
   spec but untested against this server. If "View PDF" 404s or comes back
   empty, check what `GET Binary/<id>` with `Accept: application/fhir+json`
   actually returns.
3. **Country code lookup** (`_find_country_code`) — see Geography above.
4. **scispaCy + UMLS linking** — verified end-to-end against synthetic
   clinical text (real model, real cached UMLS KB, real grouping/rendering),
   but not yet against an actual IG report PDF; expect some noise (header/
   boilerplate fragments) and check whether `linker_threshold` (0.85) sends
   too much — or too little — to "Unlinked" for real report wording.
5. **Stats date search params** (`authored`/`date`) are correct per FHIR spec
   but this server's indexing of them wasn't confirmed — if `/stats` comes
   back empty for a range known to have data, test the same query directly
   against the FHIR endpoint.
6. **`_include`/`_include:iterate` support** — used by `lab_orders_for_patient`/
   `lab_reports_for_patient` to bundle requester/specimen/result resources
   into one query (see Reference resolution above). Both calls fall back to
   per-resource GETs if the server ignores or errors on these params, so
   correctness doesn't depend on it, but if patient pages are slower than
   expected, confirm the server actually honours `_include` and `:iterate`.
7. **ctDNA text matching** (`_is_ctdna_order` / `CTDNA_TEXT_MATCHES`) — no
   confirmed code for ctDNA testing in this IG, so orders are matched by
   `code` text. If `/ctdna` comes back empty against a real server, check
   what `code.text`/`code.coding[].display` actually says on a known ctDNA
   order and adjust the match list (or switch to an exact code check).
8. **`_revinclude=DiagnosticReport:based-on`** (used by `ctdna_orders()`) —
   falls back to no linked report at all (not a per-order GET) if a server
   doesn't support `_revinclude` at all, since there's no equivalent of the
   patient-page fallback here. If reports never show up on `/ctdna`,
   confirm the server supports this search modifier. (Separately, a server
   that supports `_revinclude` but doesn't tag `Bundle.entry.search.mode`
   on the results is handled — see the ctDNA summary section above — but
   worth knowing this is a real quirk this app has hit.)
9. **ODS code system URI** (`organisation_ods_code`) — assumes
   `identifier.system == "https://fhir.nhs.uk/Id/ods-organization-code"` on
   a resolved Organization, falling back to any system-less identifier.
   Not confirmed against a real server; if ODS codes never appear next to
   organisation names on `/ctdna`, sample a real `Organization.identifier`
   array and adjust.
10. **iGene report identifier location** (`igene_report_identifier`) —
    checks the order's `identifier` list, then falls back to the report's;
    not confirmed which resource this server actually carries it on (or
    whether it's populated at all). If the "iGene report ID" column is
    always "—", check a real order/report pair directly.

## Natural next steps (not yet implemented)

- Pagination on patient pages (currently capped at 50 records per resource
  type; `/stats` already paginates via `_search_all`).
- Structured variant data (a `Genomics-Variant` FHIR profile, or
  `pdfplumber.extract_tables()` against a report's findings table) instead of
  free-form keyword scanning.
