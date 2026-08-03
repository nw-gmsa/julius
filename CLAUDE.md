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
# If the server has a real (non-self-signed) TLS cert, turn verification on:
# export FHIR_VERIFY_SSL=true

python3 app.py   # serves on http://localhost:5050 (override with PORT env var)
```

Then sign in at `/login` with FHIR server credentials — see Authentication
below. There's no seeded account; whatever you type is checked straight
against `FHIR_BASE_URL`.

There is no test suite, linter, or build step in this repo — it's a two-file
Flask app plus Jinja templates.

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

### Authentication (login screen, per-user `FhirClient`)

There is no app-level user database — `/login` (`app.py`) takes a
username/password and checks them by building a `FhirClient(user=...,
password=...)` and calling `FhirClient.verify_credentials()` (a minimal
authenticated `GET Patient?_count=1`) against `FHIR_BASE_URL`; whatever the
FHIR server accepts, the app accepts. This **replaced** the old model of one
shared `FhirClient()` built at import time from `FHIR_USER`/`FHIR_PASSWORD`
env vars — those two env vars are no longer read by the running app (only
`FHIR_BASE_URL`/`FHIR_VERIFY_SSL` still are); `fhir_client.py`'s
`os.environ.get("FHIR_USER", "sqluser")`-style defaults only matter now for
directly-constructed clients like `scripts/fix_organization_names.py`.

- The successful login's `FhirClient` (and so its password) is kept
  **server-side only**, in a module-level dict `app._session_clients`
  keyed by a random token (`secrets.token_urlsafe`); the browser's session
  cookie holds only that token plus the username for display — never the
  password itself.
- `app.client` — the name almost every route/helper in `app.py` was already
  written against — is now a `werkzeug.local.LocalProxy` resolving to
  `g.client` per-request, rather than a plain module-level `FhirClient()`.
  This was a deliberate choice to keep the ~100 existing `client.xxx` call
  sites unchanged rather than threading a client through every function
  signature — the same pattern Flask itself uses for `request`/`session`.
- `app._load_client()` (a `before_request` hook) looks up the caller's
  `FhirClient` from `session["sid"]` and sets `g.client`, redirecting to
  `/login?next=<path>` if there isn't one — every route is protected by
  default; only `login` and `static` are exempted
  (`LOGIN_EXEMPT_ENDPOINTS`).
- **No expiry beyond `/logout`** — `_session_clients` entries live for the
  life of the process once created. Fine for a small internal app on one
  long-lived Waitress process (see
  `docs/windows-iis-deployment.md`), but a lot of logins left open would
  leak memory; add a cleanup pass if that becomes a real problem.
- `app.secret_key` falls back to a random key (`secrets.token_hex(32)`) if
  `SECRET_KEY` isn't set — works fine single-process, but invalidates every
  session on restart. Set `SECRET_KEY` for production deployments.

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

### Search by order/report number

The index page's second search box (`order_number`) looks a value up
against **both** `ServiceRequest.identifier` and `DiagnosticReport.identifier`
(`FhirClient.find_orders_by_identifier()`/`find_reports_by_identifier()`,
plain FHIR `identifier` search — matches any identifier value regardless of
system, same convention `search_patients(nhs_number=...)` already uses) —
the caller doesn't know upfront whether it's a placer/filler order number
or a report identifier (e.g. iGene), so both are searched and whatever
matches is shown. `app._find_by_order_or_report_number()` resolves each
match's patient (`patient_for()`) and, when every match resolves to the
same one patient (the common case), the route redirects straight to
`/patient/<id>` rather than showing an intermediate results page. If
matches span more than one patient, or a patient can't be resolved, the
index page instead renders a disambiguation table (order/report type, test
name, patient, a link per row) rather than guessing which one to redirect
to.

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
- **"Orders by requesting organisation" also renders as a Leaflet map**
  (OpenStreetMap tiles, loaded client-side from unpkg/OSM — needs outbound
  internet from the browser) above the existing table, one circle marker
  per distinct requesting Organization (area proportional to order count).
  `app.orders_by_organisation_geocoded()` groups orders by the Organization
  resource resolved via `order_organisation_resource()`/
  `order_organisation_ods()` (same resolution chain as the ctDNA summary's
  organisation column), then geocodes each org's postcode
  (`FhirClient.organisation_postcode()` — `Organization.address[].postalCode`,
  **unconfirmed whether this server populates it at all**) via
  `FhirClient.geocode_postcode()`, which calls the free
  [postcodes.io](https://postcodes.io) API (no key required) — this needs
  outbound internet from wherever the Flask app itself runs, separate from
  the browser's tile-fetching. Geocoding results are cached per-postcode on
  the `FhirClient` instance for the process lifetime. An organisation whose
  postcode is missing or fails to geocode is still counted (`order_by_org`'s
  table is unaffected) but has no marker; `order_map_unmapped_count` surfaces
  how many orders that affected rather than silently dropping them from the
  map.
- **"Orders by patient ICS" also renders as a Plotly Express choropleth**
  (server-rendered: `app.ics_choropleth_html()` builds the figure with
  `plotly.express.choropleth()` and returns
  `fig.to_html(full_html=False, include_plotlyjs="cdn")`, embedded directly
  in `stats.html` via `|safe`) shading each matched NHS Integrated Care
  Board by order count, above the existing table.
  `FhirClient.fetch_icb_boundaries()` fetches NHS ICB polygons from the ONS
  Open Geography Portal's public ArcGIS FeatureServer (no key required;
  needs outbound internet from wherever the Flask app runs — same
  requirement as the organisation map's postcodes.io geocoding, but a
  different host), cached at class level for the process lifetime (~42
  polygons — not worth re-fetching per request; a failed fetch is **not**
  cached, so the next `/stats` request retries).
  `app._normalize_icb_name()` fuzzy-matches each resolved ICS name (from
  `patient_ics()` — i.e. whatever this server's
  `Patient.managingOrganization.name` says) against the ONS boundaries'
  official `ICB23NM` field by stripping a leading "NHS", a trailing
  "Integrated Care Board"/"ICB", normalising "&" to "and" (many of the 42
  official names are "X and Y"/"X, Y and Z" compounds — this one
  substitution alone accounts for a lot of otherwise-missed matches), and
  dropping remaining non-alphanumeric characters down to a lowercase core;
  if that doesn't produce an exact match, `app._best_icb_match()` falls
  back to a difflib `SequenceMatcher` similarity ratio against every ICB
  name (threshold `ICB_FUZZY_MATCH_THRESHOLD` = 0.82), which — unlike a
  plain substring check — still matches when a filler word like "the" is
  inserted/dropped in the middle of a name (e.g. "Cornwall and Isles of
  Scilly" vs. the official "...and **the** Isles of Scilly", where neither
  is a contiguous substring of the other). Calibrated so genuine near-
  matches score at/near 1.0 while unrelated org names score well under
  0.5, and even the five "North/South/East/West/Central London" ICBs
  (which differ by one directional word) separate cleanly. **This server's
  exact ICS naming wording against the ONS names is still unconfirmed**,
  so if the map comes up empty (or `order_ics_map_unmatched_count` covers
  everything), the muted paragraph under each map lists the actual
  unmatched ICS name strings (`order_ics_map_unmatched_names`/
  `report_ics_map_unmatched_names`) — compare one of those directly
  against a boundary's `ICB23NM` rather than needing to add a print
  statement. Every ICB in the boundary dataset is included as a row (0-count
  for unmatched ones, not just the matched subset), so all ~42 outlines
  draw and tile into the England outline rather than only the handful with
  data floating with no context; `update_geos(visible=True, ...)` also
  turns on a UK/Europe basemap (coastlines, country borders, land/ocean
  fill) underneath, and `update_traces(marker_line_color=...)` draws a
  visible border on every ICB polygon (matched or not).
- **The reports side gets the same two treatments**, on a "Reports by
  ordering provider" section (new — distinct from the pre-existing
  "Reports by performing organisation", which is who *produced* the
  report, from `DiagnosticReport.performer` via `report_organisation()`)
  and "Reports by patient ICS":
  - "Ordering provider" is who *ordered* the test — resolved via
    `order_for_report(report)` (the report's originating `ServiceRequest`,
    same lookup the Cepheid screen uses) and then the same
    `order_organisation()`/`order_organisation_resource()` chain the
    orders side uses. `orders_by_organisation_geocoded()` is reused
    unchanged for the map (it just takes a plain list of ServiceRequest
    resources — the caller passes each report's originating order instead
    of the order itself, so counts land per-report). `report_rows` grew an
    `ordering_provider` field (alongside the pre-existing `organisation`
    field, which stays the *performing* org) so `report_by_ordering_provider`
    can use the same `group_count()` table pattern as everywhere else.
  - The ICS choropleth is `ics_choropleth_html()` called on
    `group_count(report_rows, "ics")` exactly as the orders side calls it
    on `order_rows` — no report-specific logic needed, since ICS comes from
    the patient either way.
  - Map div IDs are namespaced (`org-map` for orders, `report-org-map` for
    reports) since both Leaflet maps render on the same page.

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
  `app.group_rows_by_organisation()` (alphabetical, with "Unknown" last).
  The organisation resource itself comes from
  `FhirClient.order_organisation_resource()`,
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

**Test orders only** (not Work orders) has two extra pieces, both added to
`_order_worklist`'s `order_patient` dict since `patient_for()` is already
resolved there once per order (so adding these costs no extra HTTP calls —
`resolve_reference()` is cached — even though only `test_orders.html`
currently renders them; Work orders' `order_patient` just carries the
unused key harmlessly):

- **`nhs_range_flag`** — `FhirClient.nhs_number_in_ranges(patient)`, a
  per-patient check extracted from `patients_in_nhs_number_ranges()` (which
  now just filters on it) so the same 400,000,000–499,999,999/
  600,000,000–799,999,999 range check is defined once. Rendered as a
  "Real NHS number" badge next to the patient name.
- **Delete orders with unknown patient** (`/test-orders/
  clear-down-unknown-patient`, destructive) — `test_orders()` computes
  `unknown_patient_count` straight from `order_patient` (no extra
  fhir_client call needed for the count); the delete route calls
  `FhirClient.orders_with_unknown_patient(orders)` /
  `clear_down_orders_with_unknown_patient(orders)`, which filter on
  `patient_for(order) is None` — broader than `orphaned_service_requests()`
  (wholly-absent `subject` only), since a present-but-dangling reference
  counts too. Both delete methods for ServiceRequests
  (`clear_down_orphaned_service_requests()` and this one) now share a
  `_delete_service_requests(orders)` helper. Single POST, no separate
  confirm route, same reasoning as the admin screen's orphaned-SR delete:
  no patient identity involved, and the "Unknown" cells are already
  visible on the page before the button is reachable.
  `admin_clear_down_result.html` (the shared result template) now takes
  optional `back_url`/`back_label` params so this route's result page
  links back to `/test-orders` instead of `/admin`.

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
rather than aborting on the first rejected delete). Observation resources
are deliberately left alone.

The Patient resource itself is **opt-in**: the confirm form has an
unchecked-by-default "also delete the Patient resource itself" checkbox
(`delete_patient_record`). `app.patient_clear_down()`'s POST branch calls
`clear_down_patient_and_record()` (same as the admin screen's per-patient
delete) if it's ticked, or plain `clear_down_patient()` if not — same
distinction as the admin screen, just surfaced as a checkbox here instead
of being always-on.

**GET vs POST is the safety mechanism, not an afterthought**:
`app.patient_clear_down()` handles both methods on the same route — `GET`
only fetches and displays what *would* be deleted
(`patient_clear_down_confirm.html`), `POST` (the confirm button's form)
is the only path that calls a `_delete()`-backed method
(`patient_clear_down_result.html`). This is the correct way to build any
delete control (a link/crawler/back-button can trigger a GET but never a
form POST), not something layered on afterward — don't "simplify" this
into a single bare link.

This route has **no auth, no CSRF token, and no rate limiting** — same as
every other route in this app, but worth calling out specifically here
since this one is destructive and irreversible rather than read-only. Only
run this app somewhere trusted if the clear-down feature is reachable.

### Admin screen (`/admin`) — bulk/system-wide, destructive

Not linked from `base.html`'s nav (deliberately — see the comment there)
but still reachable directly at `/admin`; there's no auth gate, so this is
obscurity, not access control. Two independent clear-down actions, both
system-wide rather than scoped to one patient:

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

### Cepheid Test Results (`/cepheid-results`)

Cross-patient, system-wide, no date bound: `DiagnosticReport`s with a
BCRABL code. `FhirClient.bcrabl_reports()` matches
`coding[].code == "BCRABL"` (`_is_bcrabl_report()`/`BCRABL_CODE`) across
*any* coding regardless of `system` — a known exact code but unconfirmed
system, distinct from both the Genomic Test Directory code (confirmed
system, matched via `test_directory_code()`) and ctDNA (no confirmed code
at all, text-matched via `_is_ctdna_order()`). Query shape mirrors
`ctdna_orders()`/`active_filler_orders()`: `_include`s specimen/patient/
result plus the originating order via `_include=DiagnosticReport:based-on`
— a **forward** include this time (the report references the order via
its own `basedOn`, so no revinclude is needed, unlike `ctdna_orders()`
which searches from the ServiceRequest side and needs `_revinclude` to
reach the report) — and identifies reports by `resourceType` across
`matches + included` combined, same `Bundle.entry.search.mode` caveat as
the other system-wide queries.

`order_for_report(report)` resolves a report's `basedOn` down to the
ServiceRequest specifically (basedOn can reference other types per spec,
though this IG only ever uses ServiceRequest) — much simpler than
`ctdna_orders()`'s reverse order-id→report lookup, since here the
direction is the natural one (report → order via a direct reference, not
order → report via a reverse search).

**The results table is component-level, not Observation-level** — the
whole point of this screen. `app.component_rows(observations)` flattens
every linked Observation's `.component` array into one row per component
(`{"label", "value", "reference_range", "flag"}`), reusing `obs_value()`
directly on each component dict (a component's `value[x]` fields are
shaped the same as its parent Observation's, so no separate value
extraction was needed) rather than `obs_value()`'s existing
component-joining branch (which produces one summary string per
Observation — fine for the patient page's generic Observation table, not
granular enough for this screen's per-component rows). An Observation
with no `component` array contributes zero rows here.

**Delete reports with no component-level results**
(`/cepheid-results/clear-down-no-components`, destructive) —
`bcrabl_reports_without_components(reports)` filters to reports where
*no* linked Observation has a non-empty `component` array (i.e. exactly
the ones `component_rows()` produces nothing for);
`clear_down_bcrabl_reports_without_components()` deletes them. Single
POST, no separate confirm route — same reasoning as the admin screen's
orphaned-`ServiceRequest` delete and the test orders unknown-patient
delete: a mechanical, well-defined criterion, not tied to a specific
identifiable patient, and already visible in full on the same GET page.
This is also where `_delete_resources(resource_type, resources)` was
introduced — the old `_delete_service_requests()` generalized to take a
resource type, since this delete targets `DiagnosticReport` rather than
`ServiceRequest`; `clear_down_orphaned_service_requests()` and
`clear_down_orders_with_unknown_patient()` were updated to call the
generalized version, no behaviour change. Reuses
`admin_clear_down_result.html` with `back_url="/cepheid-results"`.

Each row also carries **`meta.lastUpdated`** (read straight off the report,
no helper needed) and **every `identifier` on the report** via
`app.all_identifiers(resource)` — a generic "show all of them" formatter
(`"value (label)"`, label being the identifier system URI's last path
segment), distinct from the single-system identifier lookups elsewhere
(`report_identifier()`, `specimen_identifier()`, etc.) that each pick out
one specific known system. This is the one place in the app that shows a
resource's full identifier list rather than one targeted value — reuse
`all_identifiers()` rather than a bespoke loop if another screen needs the
same "just show me everything" treatment.

**Two more destructive, single-POST clear-downs** on this screen (same
mechanical/not-patient-identifying reasoning as the no-components one
above):

- **No identifiers** (`/cepheid-results/clear-down-no-identifiers`) —
  `bcrabl_reports_without_identifiers(reports)` filters on an empty
  `identifier` list; `clear_down_bcrabl_reports_without_identifiers()`
  deletes those reports **and** their associated Specimen (resolved the
  same way the screen displays it: report's own `specimen`, falling back
  to `order_for_report()`'s specimen). The specimen-safety check matters
  here: before deleting, it builds `kept_specimen_ids` from every *other*
  report in the full `reports` list (i.e. everything not being deleted),
  and only deletes a specimen if its id isn't in that set — so a
  no-identifier junk report can't take down a specimen a real, kept
  report still references. Verified with a test where two reports share
  one Specimen; the specimen survives because the other report keeps it.
- **Duplicates** (`/cepheid-results/clear-down-duplicates`) —
  `duplicate_bcrabl_reports(reports)` runs union-find over
  `_identifier_keys(report)` (the `{(system, value), ...}` set for a
  resource's identifiers) to cluster reports sharing at least one
  identical identifier — deliberately transitive, so if report A and B
  share identifier X, and B and C share a *different* identifier Y, all
  three land in one cluster (verified: a report bridging two clusters via
  a second shared identifier correctly merges them). Within each cluster
  of 2+, sorts by `meta.lastUpdated` (falling back to `issued`/
  `effectiveDateTime`) and keeps only the latest; everything else in the
  cluster is a "duplicate" to delete. Reports with zero identifiers are
  excluded from clustering entirely (they belong to the separate
  no-identifiers action, not this one — "no identifiers" isn't the same
  claim as "duplicate of a specific other report").

Both reuse `_delete_resources()` (ServiceRequest/DiagnosticReport-agnostic
by now) and `admin_clear_down_result.html` with
`back_url="/cepheid-results"`, same as the no-components delete.

### Report PDFs

`DiagnosticReport.presentedForm.url` points at a **FHIR Binary resource**
(e.g. `Binary/abc123`), not a static file. `fetch_attachment_bytes()` requests
it as `application/fhir+json` (reliably returns a JSON object with
`contentType` + base64 `data`) and decodes that, falling back to raw bytes if
a server ignores the Accept header. `/report/<report_id>/pdf` streams that
straight to the browser as the "📄 View report document" link on the patient
page.

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

1. **Auth is Basic**, using per-user credentials entered at `/login` (see
   Authentication above) rather than a fixed `FHIR_USER`/`FHIR_PASSWORD`. If
   the server moves to OAuth2/SMART (the IG's target state per its API
   Security volume), swap `_auth()` in `fhir_client.py` for Bearer-token
   support and adapt the login route to whatever that flow requires.
2. **Binary content negotiation** (`fetch_attachment_bytes`) — should work per
   spec but untested against this server. If "View PDF" 404s or comes back
   empty, check what `GET Binary/<id>` with `Accept: application/fhir+json`
   actually returns.
3. **Country code lookup** (`_find_country_code`) — see Geography above.
4. **Stats date search params** (`authored`/`date`) are correct per FHIR spec
   but this server's indexing of them wasn't confirmed — if `/stats` comes
   back empty for a range known to have data, test the same query directly
   against the FHIR endpoint.
5. **`_include`/`_include:iterate` support** — used by `lab_orders_for_patient`/
   `lab_reports_for_patient` to bundle requester/specimen/result resources
   into one query (see Reference resolution above). Both calls fall back to
   per-resource GETs if the server ignores or errors on these params, so
   correctness doesn't depend on it, but if patient pages are slower than
   expected, confirm the server actually honours `_include` and `:iterate`.
6. **ctDNA text matching** (`_is_ctdna_order` / `CTDNA_TEXT_MATCHES`) — no
   confirmed code for ctDNA testing in this IG, so orders are matched by
   `code` text. If `/ctdna` comes back empty against a real server, check
   what `code.text`/`code.coding[].display` actually says on a known ctDNA
   order and adjust the match list (or switch to an exact code check).
7. **`_revinclude=DiagnosticReport:based-on`** (used by `ctdna_orders()`) —
   falls back to no linked report at all (not a per-order GET) if a server
   doesn't support `_revinclude` at all, since there's no equivalent of the
   patient-page fallback here. If reports never show up on `/ctdna`,
   confirm the server supports this search modifier. (Separately, a server
   that supports `_revinclude` but doesn't tag `Bundle.entry.search.mode`
   on the results is handled — see the ctDNA summary section above — but
   worth knowing this is a real quirk this app has hit.)
8. **ODS code system URI** (`organisation_ods_code`) — assumes
   `identifier.system == "https://fhir.nhs.uk/Id/ods-organization-code"` on
   a resolved Organization, falling back to any system-less identifier.
   Not confirmed against a real server; if ODS codes never appear next to
   organisation names on `/ctdna`, sample a real `Organization.identifier`
   array and adjust.
9. **iGene report identifier location** (`igene_report_identifier`) —
   checks the order's `identifier` list, then falls back to the report's;
   not confirmed which resource this server actually carries it on (or
   whether it's populated at all). If the "iGene report ID" column is
   always "—", check a real order/report pair directly.
10. **Organization.address on this server** (`organisation_postcode`, used by
    the `/stats` requesting-organisation map) — untested whether
    Organization resources here carry an `address` with `postalCode` at
    all. If the map never shows any markers (`order_map_unmapped_count`
    equals the total order count), sample a real Organization resource and
    check where its address actually lives.
11. **ICS name wording vs. ONS ICB23NM** (`app._normalize_icb_name()`/
    `_best_icb_match()`, used by the `/stats` ICS choropleths) — this
    server's `Patient.managingOrganization.name` values haven't been
    compared against the ONS Open Geography Portal's official ICB names.
    If several ICS regions never shade despite having orders/reports, check
    the `order_ics_map_unmatched_names`/`report_ics_map_unmatched_names`
    lists shown under each map (no need to add a print statement) and
    compare one of those strings against a boundary feature's `ICB23NM`
    directly — if it's a wording variant the current normalisation/fuzzy
    match doesn't already handle (like the "&" vs "and" and dropped-"the"
    cases it was fixed for), extend `_normalize_icb_name()` or tune
    `ICB_FUZZY_MATCH_THRESHOLD`.

## Maintenance scripts (`scripts/`)

Standalone, run-manually scripts — not wired into the Flask app or its
nav, unlike the admin/clear-down screens.

- **`fix_organization_names.py`** — finds every Organization resource on
  the configured server with no `.name` (`FhirClient.
  organizations_without_name()`, a system-wide `_search_all()` query) and
  backfills one from the NHS ODS lookup API
  (`https://directory.spineservices.nhs.uk/ORD/2-0-0` — a plain JSON REST
  API, not FHIR-shaped; open access, no key/onboarding required per NHS
  Digital's API catalogue — the older FHIR-shaped
  `directory.spineservices.nhs.uk/STU3/Organization` endpoint some docs
  still reference has been retired) using the Organization's own ODS code
  identifier (`organisation_ods_code()`). An Organization with no ODS code
  at all, or one whose code the lookup API doesn't recognise, is reported
  separately rather than silently skipped.
  **Dry-run by default** — prints what it would change; `--apply` is
  required to actually `PUT` the corrected name back
  (`FhirClient.update_organization_name()`, via the new generic `_put()`
  helper — the app had no write path before this, only `_get`/`_delete`).
  The name is written exactly as ODS returns it (upper case, per ODS
  convention), not re-cased.

## Natural next steps (not yet implemented)

- Pagination on patient pages (currently capped at 50 records per resource
  type; `/stats` already paginates via `_search_all`).
