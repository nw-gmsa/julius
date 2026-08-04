# Lab Explorer

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
# If the server has a real (non-self-signed) TLS cert, turn verification on:
# export FHIR_VERIFY_SSL=true

python3 app.py
```

Then open **http://localhost:5050** and sign in with your own FHIR server
credentials at the login screen — there's no separate app account, whatever
you enter is passed straight through as HTTP Basic auth to `FHIR_BASE_URL`
(via `FhirClient.verify_credentials()`), so a wrong username/password shows
an error on the login page instead of getting you in. Set `SECRET_KEY` to a
fixed value in production so logged-in sessions survive an app restart
(see [`docs/windows-iis-deployment.md`](docs/windows-iis-deployment.md)) —
without it a random key is generated per process and everyone's logged out
on restart.

After signing in, search by patient name, NHS
number, or FHIR patient ID, then click through to see that patient's
genomic test orders and reports. Use the **Daily stats** link for a
day-by-day breakdown of orders/reports across all patients, the
**ctDNA summary** link for a cross-patient list of ctDNA order/result
turnaround, or the **Work orders** link for a cross-patient worklist of
active filler-order test orders.

## Deploying

For running this in production behind IIS on Windows Server (Waitress +
NSSM Windows Service + IIS reverse proxy), see
[`docs/windows-iis-deployment.md`](docs/windows-iis-deployment.md).

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
  (circulating tumour DNA) tests, matched by a confirmed Genomic Test
  Directory code (`M4.14` — `FhirClient.CTDNA_TEST_DIRECTORY_CODES`) where
  present, falling back to a text match on the order's code ("ctDNA",
  "circulating tumour/tumor DNA", "cfDNA", etc. — `CTDNA_TEXT_MATCHES`) for
  orders that don't carry that code. `_is_ctdna_order()` in
  `fhir_client.py` checks code first, then text — add more codes to
  `CTDNA_TEST_DIRECTORY_CODES` as they're confirmed against your server.
  The FHIR queries themselves also explicitly filter by this code
  (`code=https://fhir.nhs.uk/CodeSystem/England-GenomicTestDirectory|M4.14`)
  as a supplementary query alongside the existing `category`-based one, for
  the outstanding and completed-without-report buckets — in case
  `category` isn't populated reliably on your server. The initial view
  shows **outstanding orders** (any `ServiceRequest.status`
  other than `completed`) ordered within a date range, plus **orders
  completed within that same range** (bounded by the linked report's
  `issued` date, or the order date if no report resolved) — a `start`/`end`
  picker at the top of the page, same `?start=&end=` query params as
  `/stats`, defaulting to the last 30 days. The picker feeds straight into
  `ctdna_orders(start, end)`'s FHIR queries themselves (not just post-fetch
  filtering) — it used to be one unbounded `ServiceRequest` query pulling back
  *every* ctDNA order this server had ever seen, which is exactly the kind of
  thing that can trip a `413` from the FHIR server on a large history. It's
  now three: (1) outstanding orders, bound by `authored` to the same range —
  originally left unbounded ("show every outstanding order regardless of
  age"), but a live server with a large backlog of long-lived non-completed
  orders hit the exact same 413 this way, so it's bounded too now, meaning a
  very old still-active order only shows up if it falls in the selected
  range; (2) completed orders
  with a linked report, queried from the *DiagnosticReport* side and bound by
  its own `issued`/`date` (matching what `completion_date` actually filters
  by); (3) completed orders with no report at all, bound by the
  ServiceRequest's own `authored` as a fallback. Each paginates like `/stats`
  does (same 1,000-record cap) and pulls in specimen/patient/requester plus
  any linked `DiagnosticReport` via `_include`/`_revinclude` in the same
  query. Since some servers don't reliably tag `Bundle.entry.search.mode` on
  `_include`/`_revinclude`'d entries (which would otherwise misfile a
  linked report as if it were an order — the fix for a real bug reported
  against this screen), orders and linked reports are identified by
  `resourceType` across the whole pooled result set rather than by trusting
  that tag. Rows are
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
- **Cepheid Test Results** (`/cepheid-results`) shows `DiagnosticReport`s
  with a **BCRABL** code (`bcrabl_reports()`) — BCR-ABL1 quantitative
  monitoring results, e.g. from a Cepheid GeneXpert. Matched by
  `coding[].code == "BCRABL"` regardless of `system` (`_is_bcrabl_report()`)
  since the exact code is known but not which coding system carries it on
  this server — unlike the Genomic Test Directory code (a confirmed
  system) or ctDNA (no confirmed code at all, so text-matched instead).
  System-wide, no date bound. Each report is shown with its originating
  order (via `basedOn` → `order_for_report()`), specimen,
  **`meta.lastUpdated`**, **every `identifier` on the report** (not just
  the iGene one — `all_identifiers()` in `app.py`, formatted as "value
  (label)" where label is the identifier system's last path segment, for
  a short readable tag rather than the full system URI), and a **results
  table built from every linked Observation's `component`
  entries** rather than the Observations' own top-level values
  (`component_rows()` in `app.py`) — this screen is specifically about
  the BCR-ABL1/ABL1-control/%IS breakdown carried as components on a
  panel-style Observation, not a single result value. An Observation with
  no `component` array contributes nothing to this table. A **"Delete
  reports with no component-level results"** button (destructive,
  irreversible; single `POST`, no separate confirm — same reasoning as the
  admin screen's orphaned-`ServiceRequest` delete) removes every currently-
  listed BCRABL report where *none* of its linked Observations carry a
  `component` array at all (`bcrabl_reports_without_components()`/
  `clear_down_bcrabl_reports_without_components()`) — i.e. exactly the
  reports whose card already shows "No component-level results found" on
  this same page. Two more destructive, single-`POST` actions on the same
  screen, same reasoning:
  - **"Delete reports with no identifiers (and their specimens)"** —
    `bcrabl_reports_without_identifiers()`/
    `clear_down_bcrabl_reports_without_identifiers()` deletes every report
    with an empty `identifier` list, plus its associated specimen (the
    report's own `specimen`, falling back to the linked order's) — but
    **only** if that specimen isn't also referenced by another report
    that's being kept, so cleaning up junk reports can't silently break a
    real one's specimen link.
  - **"Delete duplicate reports"** — `duplicate_bcrabl_reports()`/
    `clear_down_duplicate_bcrabl_reports()` clusters reports that share at
    least one identical `identifier` (via union-find, so reports linked
    transitively through *different* shared identifiers still end up in
    one cluster), keeps the most-recently-updated report in each cluster
    (by `meta.lastUpdated`, falling back to `issued`/
    `effectiveDateTime`), and deletes the rest. Reports with no
    identifiers at all never count as duplicates of each other here —
    that's the separate action above.
- **Work orders** (`/work-orders`) is a cross-patient worklist of active
  test orders — `ServiceRequest` with `intent=filler-order` and
  `status=active`, system-wide (`active_filler_orders()`), i.e. orders as
  seen from the filler/lab system's side rather than the requesting
  system's. Same table layout and `basedOn` chain rendering
  (`build_order_chains()`) as a patient page's "Genomic test orders" table,
  plus a Patient column since it spans multiple patients. Bounded to
  `ServiceRequest.authored` within a `start`/`end` date range (same
  `?start=&end=` query params as `/stats`, defaulting to the last 30
  days) — **required**, not optional, unlike `/ctdna`'s range: this used
  to be a fully unbounded system-wide query, which could 413 a server
  with a large active-order backlog the same way the ctDNA screen's
  outstanding-orders query did (see `_active_orders_with_intent()`).
  Also has a "Requested by"/"Test" filter (organisation and Genomic Test
  Directory code, each "All" or one specific value) and a sortable
  "Ordered" column header, both client-side over the fetched date-bounded
  set — no splitting by organisation the way `/ctdna` does.
- **Test orders** (`/test-orders`) is the placer-side counterpart to Work
  orders — same screen, same date range/org/test filter and sortable
  "Ordered" column, same `active_filler_orders()`/`active_placer_orders()`
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
    same page before the button is reachable. Scoped to the same
    date/org/test filter the page was showing (passed through as hidden
    form fields), so the "N of the orders above" count it deletes matches
    what was actually on screen.
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
- **Report PDFs** — `presentedForm.url` points at a **FHIR Binary
  resource** (e.g. `Binary/abc123`), not a static file.
  `fetch_attachment_bytes()` requests it as `application/fhir+json` (which
  reliably returns the Binary resource — a JSON object with `contentType`
  and base64 `data`) and decodes that, falling back to using raw bytes
  directly if a server ignores the Accept header. The "📄 View report
  document" link on each report streams that PDF straight to the browser.
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

1. **Auth** is Basic, using whatever username/password each user enters at
   the app's own login screen (`FhirClient.verify_credentials()` confirms
   they're accepted by the FHIR server before the session is created) — not
   a fixed `FHIR_USER`/`FHIR_PASSWORD` shared by everyone any more. If the
   server later moves to OAuth2/SMART (the IG's own API Security volume,
   https://nw-gmsa.github.io/en/api-security.html, describes SMART-on-FHIR
   + IHE IUA as the target state), swap `_auth()` in `fhir_client.py` for
   Bearer-token support, and the login screen for wherever that flow directs
   a user (its own IdP login page, an OAuth redirect, etc.).
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
8. **ctDNA text matching** — `/ctdna` identifies ctDNA orders by checking
   `ServiceRequest.code`'s text for "ctDNA"/"circulating tumour DNA"/"cfDNA"
   etc. (`CTDNA_TEXT_MATCHES` in `fhir_client.py`), since I don't have a
   confirmed Genomic Test Directory code for it. If your server's ctDNA
   tests come back empty on this screen, check what `code.text`/
   `code.coding[].display` actually looks like for a real ctDNA order and
   either adjust the match list or switch to an exact code check.
9. **Completed-orders date range** on `/ctdna` defaults to the last 30 days
   but is now picker-configurable (bounded by the linked report's `issued`
   date, or the order date if no report resolved) — same `?start=&end=`
   query params as `/stats`. Outstanding orders (anything not
   `status: completed`) are shown with no date bound at all regardless of
   the picker, which could be slow or show a lot of very old orders if this
   server has long-lived active `ServiceRequest`s that were never marked
   completed.
10. **ODS code system URI** — `organisation_ods_code()` looks for
    `identifier.system == "https://fhir.nhs.uk/Id/ods-organization-code"` on
    the resolved Organization, falling back to the first identifier with no
    `system` at all. I haven't confirmed this is the system value this
    server's Organization resources actually use — if ODS codes never show
    up next to organisation names on `/ctdna`, print a sample
    `Organization.identifier` array and adjust.
11. **iGene report identifier location** — `igene_report_identifier()`
    checks the `ServiceRequest`'s `identifier` list first, then the linked
    `DiagnosticReport`'s, for one with system
    `https://fhir.nwgenomics.nhs.uk/iGene/ReportIdentifier`. I don't know
    which resource this server actually populates it on (or whether it's
    populated at all) — if the "iGene report ID" column is always empty,
    check a real order/report pair directly.
12. **BCRABL code system** — `_is_bcrabl_report()` matches
    `coding[].code == "BCRABL"` regardless of `system`, since I don't know
    which coding system this server puts it under (NGTD, LOINC, a local
    code, or all three on the same report). If `/cepheid-results` comes
    back empty against a real server, check a known BCR-ABL report's
    `code.coding` directly and confirm the code value really is `BCRABL`
    (case-sensitive exact match here, unlike ctDNA's text search).
13. **BCR-ABL results assume component-level values** — `component_rows()`
    only reads `Observation.component`, not a top-level `value[x]` on the
    Observation itself. If a server reports BCR-ABL1/ABL1/%IS as separate
    Observations each with their own top-level value (no components at
    all), this screen's results table will show nothing for that report —
    untested against a real Cepheid result, since I don't have one to
    check the actual resource shape against.

## What I'd extend first

1. **Pagination on patient pages** — right now they grab up to 50
   records per resource type and stop; for patients with long
   histories you'll want to follow the `Bundle.link[rel=next]` URL
   (the stats screen already does this via `_search_all`).
