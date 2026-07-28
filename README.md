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

Then open **http://localhost:5050**. Search by patient name, NHS
number, or FHIR patient ID, then click through to see that patient's
genomic test orders and reports. Use the **Daily stats** link for a
day-by-day breakdown of orders/reports across all patients.

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
  repeatedly across a day's worth of orders.
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

## What I'd extend first

1. **`basedOn` chains** — the IG uses `ServiceRequest.basedOn` to
   link reanalysis/cascade-testing requests to their parent order.
   Rendering that as a chain rather than a flat list would make
   multi-stage genomic workflows much clearer (and would make the
   stats screen's indication lookup for reports more direct, too).
2. **Pagination on patient pages** — right now they grab up to 50
   records per resource type and stop; for patients with long
   histories you'll want to follow the `Bundle.link[rel=next]` URL
   (the stats screen already does this via `_search_all`).
3. **Cross-tab the stats** — currently day/organisation/indication
   are three separate breakdowns; a day-by-organisation or
   day-by-indication pivot table would show trends over time rather
   than just range totals.
4. **`_include`/`_revinclude`** — patient pages still resolve
   Observations, specimens, and requesters with one HTTP call each;
   bundling related resources into the original query would cut this
   down substantially, and would speed up the stats screen's org
   resolution too (despite the in-process cache).
