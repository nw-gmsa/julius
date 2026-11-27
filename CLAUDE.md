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

# Only needed for /order/new's "Send to ESB" button (see "Order creation"
# below) — never hardcoded, no default:
# export ESB_CLIENT_ID="..."
# export ESB_CLIENT_SECRET="..."
# If the authorization server rejects the token request with 400
# invalid_scope/invalid_request (check the error message — it now
# surfaces the server's own error_description), it likely requires an
# explicit scope for this client:
# export ESB_SCOPE="..."
# ESB_BASE_URL (e.g. https://192.168.1.56/healthconnect) is the base for
# both ESB URLs: token = ESB_BASE_URL + "/oauth2/token", $process-message
# = ESB_BASE_URL + "/ESB/$process-message".

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
authenticated `GET Patient?_summary=count`) against `FHIR_BASE_URL`;
whatever the FHIR server accepts, the app accepts. `_summary=count` (not a
plain `_count=1` search) is deliberate — a `_count=1` search still asks the
server to find and build actual Patient resources before truncating to 1,
which on a server with a large Patient table is the same failure mode that
used to 413 the ctDNA screen (see `ctdna_orders()`'s history below): a
login-time 413 that looks like it's about credentials but is really the
server choking on result-set size. `_summary=count` asks the server to
return just the total and skip materializing resources entirely, enough to
prove the credentials were accepted. This **replaced** the old model of one
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

### 413s on unfiltered system-wide searches (`fhir_client.py`)

`import_econcur()`'s `Practitioner` preload, and separately
`patients_in_nhs_number_ranges()`'s `Patient` search (the admin screen's
test-patient finder), both 413 against a real server — this
deployment's FHIR server is a **HealthConnect CDR** (Clinical Data
Repository, per `FHIR_BASE_URL`'s `/healthconnect/cdr/fhir/r4` path) in
front of an **InterSystems IRIS** database (see `iris_client.py`/
`/data-quality` for the separate IRIS *SQL* connection this app also
has). Two theories were tried and ruled out by testing against the real
server before the actual cause was confirmed:

1. ~~The result page itself (`_count=100`) is too large to
   materialize.~~ Ruled out: `_get_with_count_backoff()`'s `_count`
   halving (down to `FhirClient.MIN_SEARCH_COUNT` = 5) made no
   difference.
2. ~~The server rejects a search with no real filter parameter, only
   `_count`.~~ Ruled out: `_search_all_split()`'s
   `UNFILTERED_SEARCH_FALLBACK_PARAMS` fallback (a dummy always-true
   `_lastUpdated` filter) still 413'd even with the parameter genuinely
   present in the request.
3. **Confirmed, via `_raise_for_status_with_detail()`'s surfaced error
   text** ("The page was not displayed because the request entity is
   too large") **and directly from someone who knows this deployment**:
   a CDR aggregates data federated from multiple source
   systems/organisations, and a query with no organisation scope at all
   is too expensive for it to fan out — regardless of `_count` or an
   unrelated dummy parameter, since neither actually narrows which
   source systems need to be queried. The fix is **organisation-scoped
   batching**, not a smaller page or an arbitrary extra parameter.

Both ruled-out mechanisms (`_get_with_count_backoff()`,
`UNFILTERED_SEARCH_FALLBACK_PARAMS`) are left in place regardless —
harmless no-ops here, and each is still a plausible real fix for some
*other* FHIR server's 413 behaviour, just not this one.
`_raise_for_status_with_detail()` (surfacing a FHIR error response's
`OperationOutcome` detail instead of just the bare HTTP status line —
see `_get()`/`_put()`/`_post()`) is what actually found the real cause
here and stays for the same reason: any future 413 (or other FHIR
error) shows *why* in this app's error banners instead of an opaque
status code.

**The actual fix — `FhirClient._search_all_by_organization()`** — scopes
a search to each Organization on the server one at a time (via the
standard `organization` search param: `Patient.managingOrganization` /
`PractitionerRole.organization`), pooling the results, instead of one
unfiltered system-wide search:

- `import_econcur()` preloads its `practitioners_by_gmc`/`roles_by_key`
  matching dicts via one `PractitionerRole?organization=...&_include=
  PractitionerRole:practitioner` search per Organization (replacing the
  old `all_practitioners_by_gmc()`/`all_practitioner_roles_by_practitioner_org()`,
  both now deleted — no other callers). This can't *fully* replace a
  global by-GMC lookup on its own, though: a Practitioner who already
  exists but whose only role is at an organisation this preload can't
  see (e.g. no ODS-code identifier on that Organization, so it's not in
  `all_organizations_by_ods()`'s dict at all) wouldn't be found by it.
  `_import_econcur_row()` covers that gap with `_find_practitioner_by_gmc()`
  — a single targeted `Practitioner?identifier=<gmc-system>|<gmc>`
  search (matches at most one resource, so stays well inside whatever
  makes an unscoped query too expensive) whenever the org-batched
  preload comes up empty for a GMC number, before concluding "create
  new". Found-via-fallback practitioners are cached into
  `practitioners_by_gmc` too, so a later row for the same GMC number
  (very common — one consultant can hold roles at several trusts)
  doesn't repeat the search.
- `patients_in_nhs_number_ranges()` fetches `Patient` the same way, one
  Organization at a time, plus one best-effort extra
  `Patient?organization:missing=true` search afterwards to catch
  patients with no `managingOrganization` at all (same fallback-modifier
  pattern `orphaned_service_requests()` uses for `subject:missing`;
  swallowed on failure rather than losing what the per-organisation
  searches already found).
- **`all_organizations_by_ods()` itself is still one unfiltered
  `Organization?_count=100` search** — the one remaining "fetch
  everything" query in this app, kept because there's no smaller unit to
  batch it by, and because Organization is presumably a much smaller
  table than Practitioner/Patient on this CDR (hasn't been seen to 413).
  **If it ever does**, it needs its own partition key (an ODS region
  code, alphabetical range, ...) — there's no way to batch "fetch every
  organisation" *by* organisation.

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

**The search results table (`/search`) shows each matched patient's NHS
number**, falling back to their **CHI number** (`FhirClient.CHI_NUMBER_SYSTEM`
= `https://fhir.hl7.org.uk/Id/chi-number`, Scotland's equivalent — system URI
unconfirmed against a real server, same caveat as everything else keyed off an
assumed identifier system) when no NHS number is present, via
`FhirClient.nhs_or_chi_number(patient)` (returns `(label, value)`, `label`
switching to `"CHI number"` only when that's the one actually shown, so the
column stays labelled "NHS number" in the common case but a CHI-only patient
is clearly marked rather than silently shown under the wrong label).
`app.search()` builds this once per result as `patient_numbers` (keyed by
patient id) rather than resolving it in the template.

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

### Order creation (`/order/new`) — download only, never writes to the server

Create-order screen that builds a genomic test order as a downloadable
FHIR **message Bundle** (`.json`) — laid out after NW GLH's "Genomic
Testing Request Form (Rare Disease)" (DOC4900 —
https://mft.nhs.uk/nwglh/documents/test-request-forms/), this IG's own
`ServiceRequest`/`Specimen` profiles, its `GenomicTestOrder` Questionnaire's
"Ask At Order Entry" section, and its own worked message-bundle example
(all at https://nw-gmsa.github.io/). **Nothing this screen builds is ever
written to the FHIR server** — no `_post`/`_put` call anywhere in
`FhirClient.build_order_message_bundle()`; submitting the form returns
the Bundle as a file download instead of redirecting anywhere.

**Patient and requesting organisation are never freely typed** — each is
a small search-then-pick panel backed by this FHIR server:
`FhirClient.search_patients()` (existing) and `search_organizations(name=None,
ods_code=None)`. Both refuse to run an unfiltered query (return `[]` if
neither argument is given) rather than risk the 413 an unscoped
system-wide search causes on this CDR (see "413s on unfiltered
system-wide searches" above).

**Requesting clinician is not a free search at all** —
`practitioners_for_organization(organization_id)` lists only
Practitioners this FHIR server already links to the *picked* organisation
via an existing `PractitionerRole` (`PractitionerRole.organization` —
the same linkage `import_econcur()` populates for hospital consultants),
via `_include=PractitionerRole:practitioner` (falling back to resolving
each role's practitioner reference directly if a server doesn't tag
`_include`d entries' `search.mode` reliably — the same quirk
`ctdna_orders()` already works around). The clinician section on
`order_new.html` doesn't even render until an organisation is picked.

`app.order_new()` carries all three picks across requests as
`patient_id`/`org_id`/`practitioner_id` — query params while picking,
hidden form fields once POSTing the finished order — rather than any
server-side wizard/session state; only once all three resolve
(`resolve_reference()`, which already swallows a bad/stale id into
`None`) does the rest of the order/specimen/AOE-questions form render.

**The R-code field is a `<select>` sourced from the IG's own published
GenomicTestCode CodeSystem**
(`GENOMIC_TEST_DIRECTORY_CODESYSTEM_URL` =
https://nw-gmsa.github.io/en/CodeSystem-GenomicTestCode.json, a
~2,100-entry fragment of England's National Genomic Test Directory —
same underlying codes as `GENOMIC_TEST_DIRECTORY_SYSTEM`, just where the
IG actually publishes the code/display list). `genomic_test_directory_codes()`
fetches and caches it at class level for the process lifetime — same
"static reference data, not worth re-fetching per request, failed fetch
not cached" pattern as `fetch_icb_boundaries()` (the `/stats` ICS
choropleth's ONS boundary fetch). Every option the select offers is a
real code; there's no separate free-typed test-name field any more —
`genomic_test_directory_display()` looks the display text up from the
same cached list so `ServiceRequest.code`'s text always matches what was
actually offered.

**"Ask at order entry" is a new section** rendering the IG's
`GenomicTestOrder` Questionnaire's `linkId "AskAtOrderEntry"` group
(consanguinity, pathology report confirmation, neonatal/prenatal/neither,
pregnancy-related follow-ups, pregnancy loss, deceased infant) —
hardcoded as `FhirClient.ASK_AT_ORDER_ENTRY_QUESTIONS` (a small, stable
7-question set; not worth reproducing the Questionnaire's generic
nested-item/`enableWhen` machinery for). The three pregnancy-only
follow-up questions carry a `shown_when` condition that `order_new.html`
enforces with a small bit of inline JS (show/hide, keyed off the trigger
question's `<select>` value) rather than in Python. **Each answered
question becomes its own `Observation`** (`_build_aoe_observation()`),
referenced from `ServiceRequest.supportingInfo` — not a
`QuestionnaireResponse` — matching the Observation-per-question shape the
IG's own worked example
(https://nw-gmsa.github.io/en/Bundle-GenomicsOrderMessageCodedEntries.html)
uses for these exact questions (`OBX-Consanguinity`, `OBX-Pregnancy`,
`OBX-PregnancyExpectedDeliveryDate`, ...). Each question's `value_type`
(`codeable_concept`/`date_time`/`quantity`) comes from that Questionnaire
item's own `definition` element, not guessed.

`FhirClient.build_order_message_bundle()` assembles the whole thing —
shaped after that same worked example:

- **`Bundle.type = "message"`**, a `MessageHeader` first entry (event
  `http://terminology.hl7.org/CodeSystem/v2-0003|O21`, "OML - Laboratory
  order") plus every resource it `focus`es on, exactly the worked
  example's shape.
- **Self-contained, not server-referencing**: since a message bundle
  travels to another system, nothing in it can reference our own
  server's internal ids — a receiving system has no way to dereference
  `"Patient/<our-id>"`. So the picked `Patient` resource is inlined in
  full (a real copy, not a reference), and `PractitionerRole.practitioner`/
  `.organization` (plus `MessageHeader.sender`) use **logical
  references** — `identifier` (GMC number / ODS code) + `display`, no
  `.reference` at all — via the new `_logical_reference()` helper,
  exactly how the worked example's own `PractitionerRole` entry does it.
  Every resource gets a fresh `urn:uuid:` `fullUrl`.
- **`destination`/`ORDER_MESSAGE_DESTINATION_*`** are fixed — this app is
  specifically for the North West GLH, so every order goes to the same
  place — values taken directly from the worked example (ODS `699X0`,
  "NORTH WEST GLH", endpoint `https://fhir.nwgenomics.nhs.uk/Endpoint/RIE`),
  not guessed. `source.endpoint` (`ORDER_MESSAGE_SOURCE_ENDPOINT`) is a
  placeholder — this app has no real registered `Endpoint` resource of
  its own, unlike the worked example's sending system.
- **`code` and `reasonCode` are two separate concepts, not the same
  value repeated** — `code` (the test itself) is the full R/M code
  under `GENOMIC_TEST_DIRECTORY_SYSTEM`; `reasonCode` (the clinical
  indication) is the **prefix before the "."** (e.g. `"M1"` from
  `"M1.1"`) under a different system, `GENOMIC_CLINICAL_INDICATION_SYSTEM`
  (`https://fhir.nwgenomics.nhs.uk/CodeSystem/GenomicClinicalIndication`)
  — confirmed by the worked example's own `reasonCode`
  (`{"system": ".../GenomicClinicalIndication", "code": "R240", ...}`
  for test code `"R240.1"`). `reasonCode` is *always derived from
  `test_code` itself* inside `build_order_message_bundle()`
  (`test_code.split(".")[0]`) — there's no separate form field for it to
  go out of sync with. See "Clinical indication" below for the select
  that surfaces this on the form itself. One order = one test, one
  specimen (mandatory — see below); the paper form's note that more than
  one Test Indication Code can be requested on one form isn't modelled —
  that would need multiple `ServiceRequest` resources (optionally
  sharing one `Specimen`), so submit the form again for a second test.
- **Placer order number** — an optional "Order number" field lets the
  form supply one from an external ordering system; if left blank,
  `generate_order_placer_number()` mints one itself (`"LE" + today's
  date + a random 6-hex-digit suffix`) under a local identifier system,
  `ORDER_PLACER_NUMBER_SYSTEM`
  (`https://fhir.nwgenomics.nhs.uk/Id/lab-explorer-order-number`, same
  local-system convention as `IGENE_PATIENT_IDENTIFIER_SYSTEM`/
  `SPECIMEN_IDENTIFIER_SYSTEM`) — either way it's stored as a v2-0203
  `"PLAC"`-typed identifier, the same shape `placer_identifier()` reads
  back elsewhere in this app, with `assigner` set to the requesting
  organisation (via `_logical_reference()`) when it has an ODS code.
  `app.order_new()` finds the `ServiceRequest` entry by `resourceType`
  (not a positional index) to pull this value back out for the
  download's filename.
- **Fields the paper form has that neither this IG's profiles nor the
  AOE questions model structurally** (e.g. "taken by") fold into
  `clinical_details` free text instead, same faithful-subset-only
  approach `order_view.html` documents for reading real orders back —
  the domain archetype's own "source site"/notes-shaped fields aren't
  collected by this form at all (deliberately trimmed; see below).

**Clinical indication** — a new `<select>` above the R/M code one,
populated by `genomic_clinical_indications()` (grouping
`genomic_test_directory_codes()` by the prefix before each code's ".",
using the first matching test's display text up to its first comma as
the description — e.g. `"M1.1"`'s "Colorectal Carcinoma, Multi-target
NGS panel, small variant (KRAS, NRAS, BRAF)" becomes indication `"M1"`
"Colorectal Carcinoma"; a display with no comma at all just uses the
whole thing). **Purely a client-side narrowing aid, not its own form
field** — `order_new.html` gives every R/M code `<option>` a
`data-indication` attribute (its own prefix) and a small script hides
non-matching options when an indication is picked (clearing the R/M
code selection if it no longer matches); nothing about the pick is
submitted or read server-side, since `reasonCode` is derived from
whatever `test_code` actually gets submitted anyway (see above) — the
indication select can only ever narrow to a code that's already
consistent with it.

**Specimen is mandatory, and its fields follow the Specimen profile's own
"Domain Archetype" table**
(https://nw-gmsa.github.io/en/StructureDefinition-Specimen.html#domain-archetype)
— `app.order_new()` rejects a submission with no `specimen_type` before
calling `build_order_message_bundle()`, since the IG makes
`Specimen.type` mandatory (1..1). The form's other specimen fields map
onto that same table: **Specimen ID** →
`Specimen.identifier[PlacerSpecimenNumber]` — no `system` (removed;
previously carried `SPECIMEN_IDENTIFIER_SYSTEM`, which named the
*iGene* specimen identifier specifically, not a generic placer number),
just a bare `value` plus an **`assigner`** identifying the requesting
organisation (`_logical_reference()`, same as the placer order number's
own `assigner` above) — **Specimen accession number** →
`Specimen.accessionIdentifier`, **Shipment tracking numbers** →
`Specimen.identifier[ShipmentTrackingNumber]` (LOINC `97209-1`,
confirmed by that table — a textarea, one per line, each its own
identifier, since a specimen can carry several), **Container ID** →
`Specimen.container[].identifier` (also one per line, one container
each; `FhirClient.as_value_list()` splits/de-dupes both). Loading a saved
order reads every tracking number/container id back
(`specimen_tracking_numbers()`/`specimen_container_identifiers()`, also
Jinja filters — `/order/<id>`'s Samples table shows both as columns), **Sample collection/received date** →
`Specimen.collection.collectedDateTime`/`Specimen.receivedTime`.
**Specimen source site and specimen notes are deliberately not
collected** — removed from the form (they were free text; not worth the
extra fields for this screen).

**Specimen type is a `<select>` sourced from the IG's own
`specimen-type` ValueSet**
(https://nw-gmsa.github.io/en/ValueSet-specimen-type.html) —
`FhirClient.SPECIMEN_TYPE_CODES`, the 24 SNOMED-coded concepts (hardcoded,
with `display` text — the ValueSet's own JSON only carries bare codes for
most of them, the human-readable text only exists in the rendered HTML
expansion, so a live per-request fetch wouldn't have saved anything here,
unlike `genomic_test_directory_codes()`). The ValueSet also permits any
code from a `https://fhir.nwgenomics.nhs.uk/CodeSystem/IGENE` local
codesystem for backward compatibility, but that codesystem's contents
aren't published anywhere this app can enumerate, and the ValueSet page's
own text says "SNOMED codes are preferred" — so only the SNOMED half is
offered. `Specimen.type` is built as a real coding (`{"system":
"http://snomed.info/sct", "code": ..., "display": ...}`), not free text.

**Requesting clinician is a searchable `<select>`, not a link-per-row
table** — `order_new.html` renders every `practitioners_for_organization()`
result as an `<option>` (name + GMC number, via the new `gmc_number`
Jinja filter) inside one `<select size="8">`, with a plain text input
above it that hides non-matching `<option>`s as you type (vanilla JS,
substring match on name — no framework, same minimal-JS approach as the
AOE show/hide logic above). Submitting still redirects with
`practitioner_id` set, same picker pattern as patient/organisation.

**GMC numbers are always `"C"`-prefixed**, both here and in
`import_econcur()` (which originally stored the bare digits — a bug,
fixed) — `FhirClient._format_gmc_number()` strips any existing `"C"`/`"c"`
prefix and reapplies exactly one, per
https://nw-gmsa.github.io/en/StructureDefinition-PractitionerIdentifier.html#professional-registration-entry-identifier
("CONSULTANT_CODE", format `CNNNNNNN` — confirmed by that page's own
worked example, `{"system": "https://fhir.hl7.org.uk/Id/gmc-number",
"value": "C3456789"}`). Used in two places:

- `build_order_message_bundle()` normalizes whatever GMC value is
  already stored on the picked Practitioner before building the
  `PractitionerRole.practitioner` logical reference — so the downloaded
  bundle is spec-correct even for a Practitioner imported before this
  fix existed.
- `parse_econcur_row()` now formats econcur.csv's column 0 (the bare
  digits) through `_format_gmc_number()` instead of using it as-is, so
  every *newly created* Practitioner gets the correctly-formatted
  identifier value going forward (column 1 already carries this same
  "C"-prefixed value in the source file, but deriving it from column 0
  keeps one source of truth for the format rather than trusting the two
  columns to always agree).

Since a server may already have Practitioners imported under the old,
un-prefixed format, **matching is normalized on read too**, so re-running
the import doesn't create duplicates for them: the org-batched preload
(`import_econcur()`) runs each existing Practitioner's stored GMC value
through `_format_gmc_number()` before using it as the `practitioners_by_gmc`
dict key, and `_find_practitioner_by_gmc()`'s fallback search (used when
the preload doesn't already cover a GMC number) tries the bare-digit form
too if the "C"-prefixed search comes back empty. Verified directly: a
Practitioner seeded with the old bare-digit identifier value is matched
(not duplicated) by a re-import of the same GMC number.

**Hospital number, and other-organisation medical record numbers being
stripped from the exported Patient** — `_patient_for_order_bundle()`
builds the Patient copy that actually goes into the bundle (not the raw
resolved resource): any HL7 v2-0203 `"MR"`-typed identifier whose
`assigner` isn't the requesting organisation is dropped before inlining
— a receiving lab has no business seeing a patient's hospital number at
some unrelated trust, and shouldn't be sent it. Non-MR identifiers (NHS
number, etc.) are never touched. The order-create form surfaces this as
a **"Hospital number"** field (`order_new.html`, top of the Test request
table) — pre-filled, GET-only, from any existing MR identifier the
picked Patient already has for the picked organisation
(`medical_record_numbers()`, matched by `assigner_ods`); whatever value
is actually submitted becomes (or replaces) that organisation's MR
identifier on the exported Patient, letting the user correct/supply it
rather than just silently dropping a patient's only hospital number for
this trust because the stored data doesn't have one, or has a stale one.

**"Send to ESB" — the form's second submit button, alongside "Download
order"** — POSTs the exact same bundle straight to this deployment's
ESB (Enterprise Service Bus) `$process-message` endpoint instead of
downloading it, via `FhirClient.send_order_to_esb()`. This is a
genuinely separate integration from the rest of `fhir_client.py`: a
different base URL, and its own **OAuth2 client-credentials** app
registration — not the per-user Basic-auth `FhirClient` session
everything else in this app authenticates with — so it's implemented as
classmethods needing no session/instance, with a class-level token
cache (`_esb_token_cache`, same reasoning as `_icb_boundary_cache`/
`_genomic_test_directory_cache` above: not tied to who's logged in).

- **Config is env-var only, read fresh on every call**
  (`FhirClient.esb_config()`) — both URLs are derived from
  `ESB_BASE_URL` (`+ "/oauth2/token"` / `+ "/ESB/$process-message"`,
  `ESB_TOKEN_PATH`/`ESB_PROCESS_MESSAGE_PATH`; `ESB_BASE_URL_DEFAULT` if
  unset — not a secret), but
  `ESB_CLIENT_ID`/`ESB_CLIENT_SECRET` have **no default and are never
  hardcoded** — `esb_access_token()` raises a clear `RuntimeError` if
  they're unset, shown as-is on the result page. `verify_ssl` reuses
  `FHIR_VERIFY_SSL` rather than a separate env var, since the ESB
  endpoint is the same host (192.168.1.62) as this deployment's own
  `FHIR_BASE_URL` — same TLS cert situation. `ESB_SCOPE`, if set, is
  sent as the token request's `scope` — added after a real 400 against
  this deployment's own authorization server; unset by default since
  guessing a scope value would be worse than omitting it.
- **Token flow**: client id/secret sent as HTTP Basic auth on the token
  request (`grant_type=client_credentials`, `scope` too if `ESB_SCOPE`
  is set), per this deployment's own setup — cached at class level
  (keyed by token URL, so changing `ESB_BASE_URL` never reuses another
  server's token) until 60s before `expires_in` runs out. A rejected token request
  raises `requests.HTTPError` with the authorization server's own
  `error`/`error_description` body (RFC 6749 §5.2) folded into the
  message — plain `response.raise_for_status()` only gives a generic
  "400 Client Error: Bad Request", which was the actual failure mode
  hit against this deployment's server and gave no way to tell
  `invalid_scope` from `invalid_client` from anything else without this.
  `send_order_to_esb()` retries once with `force_refresh=True` on a 401
  (a cached token that expired early or was revoked server-side) before
  giving up; any other failure (network, non-401 4xx/5xx) is raised
  as-is for `app.order_new()` to show — same "surface it, don't swallow
  it" approach as everywhere else in this app. Verified directly (mocked
  HTTP): missing credentials, the OAuth2 error body being surfaced,
  `ESB_SCOPE` being included when set, token caching (a second call
  doesn't re-authenticate), and the 401→refresh→retry sequence.
- **`app.order_new()` branches on the clicked button's `action` value**
  (`"download"` vs `"send_esb"`, both `<button type="submit"
  name="action" value="...">` in the same form) — the bundle itself is
  built identically either way; only what happens to it after differs.
  A successful send renders `order_send_result.html` with the placer
  number and the ESB's own response body (typically another message
  Bundle) pretty-printed; a failure renders the same template with
  `error` set instead — nothing is written to the FHIR server on
  either path, this route still never calls `_post()`/`_put()`.
- **The response's embedded HL7 v2 message is pulled out and shown
  separately** — `FhirClient.extract_hl7v2_message()` (verified against
  the real `examples/OrderResponse.json`) reads
  `OperationOutcome.issue[].diagnostics` from the response Bundle: this
  ESB replies with the actual HL7 v2 (`MSH|...`/`PID|...`/etc.) the FHIR
  order was converted into and sent onward as, not just an
  acknowledgement — not a documented FHIR convention, just what this
  particular deployment does. `app.order_new()` normalizes the
  segment-separating `\r`s to `\n` before rendering, so
  `order_send_result.html`'s "HL7 v2 message" `<pre>` block shows one
  segment per line; the full raw JSON response is still shown
  underneath it too ("Raw ESB response"), nothing is hidden.
  The "Send to ESB" button has a JS `confirm()` on its own `onclick`
  (not the form's `onsubmit`, which would also gate the unrelated
  "Download" button) — same destructive-action-confirmation convention
  as the admin screens', since this one has a real external side effect
  the download doesn't.

**"Load from a saved FHIR order"** (a collapsed `<details>` at the top
of the page) — uploads a previously downloaded order `Bundle` (`.json`;
`examples/genomic-order-YHCRABCDORDER.json` is a real one, and
`FhirClient.parse_order_message_bundle()` is verified directly against
it) and pre-fills the rest of the form from it, via a third POST
`action` (`"load"`) on the same route, handled *before* — and mutually
exclusive with, via `elif` — the existing build/submit branch.

- **The "always picked from this server" rule still applies** — a
  loaded bundle's inline Patient and logical-reference Practitioner/
  Organization are never used directly as `patient_id`/`org_id`/
  `practitioner_id`. `parse_order_message_bundle()` only extracts their
  *identifying* values (NHS number, ODS code, GMC number — plus
  display names, for the "not found" messages below), which
  `app.order_new()` then searches this server with
  (`search_patients(nhs_number=...)`/`search_organizations(ods_code=...)`/
  `practitioners_for_organization(org_id)` filtered by
  `_format_gmc_number()`-normalized GMC, same normalization the ESB
  bundle-building side already uses). A single match sets that id
  straight away; zero or multiple matches leave it unset and add a
  message to `load_notes` (shown in a card above the picker sections)
  explaining why and pointing at the manual search below — the
  clinician lookup is skipped entirely if the organisation itself
  didn't resolve, since `practitioners_for_organization()` needs a real
  `org_id` to query with.
- **`ServiceRequest.requester` isn't always a PractitionerRole
  reference into the same bundle** — a real producer's export
  (`examples/Liverpool_O21_Apr26.json`) sends it as a bare logical
  reference straight to the requesting Organization instead
  (`{"identifier": {ods-organization-code, ...}}`, no `.reference` at
  all, and no individually identified clinician anywhere in the
  bundle). `parse_order_message_bundle()` tries three shapes in order:
  a resolved `PractitionerRole` (the normal case), a resolved
  `Organization` directly, then a bare logical reference with no
  resolvable resource at all — this was the actual bug reported
  (organisation not picked up on import): the old code only handled
  the first shape and silently extracted nothing for the other two.
- **`ServiceRequest.supportingInfo` can point at an Observation
  "panel", not the individual answers directly** — the same Liverpool
  export's *one* `supportingInfo` entry isn't an AOE answer at all,
  it's a grouping Observation with no code/value of its own, just a
  `hasMember` list of the 14 real ones (`is_observation_panel()`/
  `flatten_observation_refs()`, the latter recursing in case a panel
  groups other panels). `parse_order_message_bundle()`'s
  `supportingInfo` walk goes through this dereference before matching
  against `ASK_AT_ORDER_ENTRY_QUESTIONS`, rather than trying to match
  the panel wrapper itself (which never matches anything, silently).
- **Observations that aren't one of `ASK_AT_ORDER_ENTRY_QUESTIONS` are
  kept, not dropped — shown read-only *and* still included in the
  output** — Liverpool's 14 panel members are exactly this case:
  free-text Q&A-style Observations, none SNOMED-coded the way the fixed
  7 questions are. `parse_order_message_bundle()` scans *every*
  Observation in the bundle for these (not just ones reachable via
  `supportingInfo`, panel-dereferenced or not — a real producer's
  export might not even link them that way at all), and only excludes
  ones already matched to a real question via the panel walk above.
  `_observation_label_and_value()` handles two shapes: the normal one
  (`code` names the question, `value[x]` carries the answer) and a
  non-conformant one the same Liverpool export uses for these extras —
  no `value[x]` at all, question text in `code.coding[0].code` and the
  answer in `code.coding[0].display` instead (backwards from normal
  FHIR convention) — falling back to the latter only when there's
  genuinely no `value[x]` to prefer. Rendered inside "Ask at order
  entry", below the editable questions, muted, only once `ready` (like
  the rest of that section) — not before, and not as a separate
  always-visible card (an earlier version showed it early specifically
  because Liverpool's clinician-pick GET used to wipe it; that's now
  fixed at the actual cause instead, see `_order_load_cache` below, so
  there's no more reason to special-case where it renders).
  **`build_order_message_bundle()`'s `extra_observations` parameter**
  round-trips each `{"label", "value"}` back out as its own simple
  `Observation` (`code.text` = label, `valueString` = value —
  deliberately not trying to reconstruct the source coding/`value[x]`
  shape, since for Liverpool's non-conformant ones there's nothing
  clean to reconstruct), referenced from `ServiceRequest.supportingInfo`
  alongside the AOE-matched ones. Without this, re-downloading or
  re-sending a loaded order would have silently dropped everything that
  didn't fit one of the 7 fixed questions — this was a real gap: these
  were only ever shown read-only, never actually included in what
  gets submitted (verified on both the download and Send-to-ESB paths).
- **`ServiceRequest.specimen` can be absent even when a `Specimen` is
  in the bundle** — Liverpool's `ServiceRequest` has no `specimen`
  element at all; the `Specimen` entry (and so its type/placer id/etc.)
  simply isn't linked from the order at all. Falls back to the one
  `Specimen` in the bundle (if there's exactly one — deliberately not
  auto-picked when there's more than one candidate, same
  don't-guess-when-ambiguous stance as everywhere else in this method)
  whose own `subject` matches the order's patient. This was the actual
  bug reported (specimen type/ID not prepopulated) — the old code only
  ever looked at `ServiceRequest.specimen`, so on this file `specimen`
  stayed `None` and every specimen field silently came back empty.
- **`ServiceRequest.note` — *every* entry, not just the first** — a
  real producer's export (same Liverpool file) puts each order-entry-
  form field into its own separate `note` (27 of them: `"**Referring
  Clinician Name:** : _Dr Natalie Canham_"` and so on) rather than one
  combined block. `clinical_details` (the "Clinical information"
  section) now joins every note's text with newlines instead of taking
  only `note[0]`, which silently dropped the other 26.
- **Everything else round-trips directly into `form_values`/
  `aoe_values`** — test code, order number, priority, clinical details,
  hospital number, every specimen field, and AOE answers (matched back
  to `ASK_AT_ORDER_ENTRY_QUESTIONS` by `(system, code)`, not just
  `code`, to avoid any cross-question collision) — verified end-to-end
  against both real example files: every one of these fields, plus all
  three resolved ids (where resolvable) and the extra read-only
  observations, comes out correct, with no regression against the
  original well-formed example (single note, panel-free, all 6 AOE
  answers, ready state reached, real submission still builds correctly).

**The loaded state survives a manual patient/organisation/clinician
pick** (`_order_load_cache`, `app.py`) — fixes what was originally a
real gap: picking one of those when it doesn't auto-resolve is a plain
GET, which carries no form body, so anything a "load" had just filled
in (test code, specimen fields, hospital number, AOE answers, the extra
observations above) would otherwise vanish the moment Liverpool's file
— which has no clinician in structured form at all — forced a manual
pick. Fixed with the same server-side-dict-keyed-by-a-random-token
pattern `_session_clients` already uses: a "load" POST stores
`parse_order_message_bundle()`'s full return value in `_order_load_cache`
under a fresh `load_token`, which then rides along as a hidden field/
query param on every picker link and form on the page (patient/
organisation/clinician search results, "Change X" links, and the main
order form) exactly the way `patient_id`/`org_id`/`practitioner_id`
already do. Whenever `load_token` resolves to a cached entry, it's
re-applied the same way a fresh load applies it — **except when the
current request is itself a `download`/`send_esb` submission**
(`resubmitting_order` in `app.order_new()`): that POST's `request.form`
already holds whatever the user actually has in the form right now,
possibly edited from what was originally loaded, and must never be
silently overwritten by the stale original values — verified directly
(the user clears/replaces a loaded field, submission fails validation
on an unrelated field, the page re-renders with the user's edit intact,
not the original loaded one). `extra_observations` has no form field at
all, so it's the one piece always re-applied from the cache regardless.
No expiry beyond the life of the process, same caveat as
`_session_clients`.

Reached from the nav ("Orders and Reports" → "New order"), or from a
patient page's "Genomic test orders" section ("+ New order for this
patient", pre-filling `patient_id` so that picker step is skipped).

### Testing menu (`/testing`) — test systems only

Nav "Testing" dropdown, hidden when `client.is_production()`; every
`/testing*` path also 404s there (`_load_client()`).
**`/testing/send-message`** — upload or paste a message, preview/edit it
(type auto-detected by `FhirClient.detect_message_type()`: JSON resource
→ FHIR, leading `MSH` → HL7 v2), then submit (JS `confirm()`):

- **FHIR** → the same ESB `$process-message` as "Send to ESB"
  (`send_fhir_message_to_esb()`, same token/401-retry, `ESB_BASE_URL`),
  but the response body is surfaced on failure; an IIS HTML error page is
  reduced to its headings. A 500 for every authenticated POST (even `{}`)
  against 192.168.1.56 was server-side, not the message — tokens from
  that host carry `iss: https://localhost/healthconnect/oauth2`.
- **HL7 v2** → HTTPS POST of the plain ER7 message (no MLLP framing,
  `Content-Type: x-application/hl7-v2+er7`, segments normalised to `\r`
  by `normalise_hl7v2()`) to `HL7V2_BASE_URL + /hl7v2/orderfiller`
  (`send_hl7v2_http()`; IIS → `OrderFillerHL7v2HttpService`, see
  RIE-SourceCode `docs/hl7v2-http-over-iis.md`). `HL7V2_BASE_URL`
  defaults to `ESB_BASE_URL`'s scheme + host (the `/hl7v2` rewrite sits
  at the IIS site root, not under `/healthconnect`). This **replaced**
  MLLP to port 9980 (`HL7V2_HOST`/`HL7V2_PORT` are no longer read). A
  Bearer token is sent only if `ESB_CLIENT_ID`/`ESB_CLIENT_SECRET` are
  set (the service authenticates only if Basic/OAuth2 is enabled on it),
  with one 401 retry. The ACK comes back with HTTP 200 either way — MSA-1
  says whether it was accepted; a non-2xx (401/403/503, IIS error) has no
  ACK and is shown as an error.

**HL7 v2 validation** (ESB backend only — `_hl7v2_validation_context()`
says so otherwise): a v2 upload gets **Submit (MLLP)** and **Validate**.
Validate POSTs the message (segments normalised to `\r`) to the same ESB
`$validate` as a FHIR `Binary` (`contentType` `x-application/hl7-v2+er7`,
`FhirClient.validate_hl7v2_with_esb()`). RIE-SourceCode's
`RIE.FHIR.Server.ValidateHL7v2()` converts it with
`RIE.Transforms.HL7v2ToFHIRMessage` — the transform live v2 messages go
through — validates the result with FHIR.Validate, and replies with a
`Parameters` (`return` = OperationOutcome, `message` = the converted
Bundle); nothing is routed or stored. An ESB without that change just
validates the Binary, which shows as "no converted FHIR message". From
there the job is a normal Bundle report (`fhir_validator.validate_hl7v2()`
for the first stage, then the same automatic follow-up), with issue line
numbers dropped (they point into the ESB's unindented JSON).
**Translating back to v2** (`hl7v2_mapping.py`): each issue gets a `v2`
location — **segment** from the resource's `meta.source`
(`urn:hl7v2:<position>-<segment>[,...]`, position = segment's place in the
message, MSH = 1), which the transform only writes when the message's
`Source` is `"$validate"` (`TagSourceSegments`, so routed messages are
unchanged); **field** from `hl7v2_mapping.FIELD_MAP` (resource type + FHIR
path → v2 field, read off the transform's `Get*` methods — keep it in step
when the transform changes). Elements the transform sets itself map to
`CONVERSION` ("Added by the HL7 v2 → FHIR conversion" — advice says the
sender can't fix these). "Unable to find a profile match" issues are
located at the referenced resource's segment. The report (page and PDF)
adds an **HL7 v2** column (field + clickable line, opening the v2 message
with that segment highlighted), "View converted FHIR" / "View HL7 v2
message", and v2-specific advice. Without `meta.source` (older ESB, or a
resource the transform made up) the segment is the nearest one of the
mapped name.

**Only a FHIR message Bundle (`Bundle.type = "message"`) can be sent** —
any other FHIR resource can be uploaded/pasted but only validated; the
send button is hidden and `action=send` rejects it server-side too.

**FHIR validation** (same page, `action=validate`) has two backends,
picked by `FHIR_VALIDATION_BACKEND`:

- **`esb` (default)** — POSTs each resource (pretty-printed, so line
  numbers match) to the ESB's **`$validate`** (`ESB_BASE_URL +
  /ESB/$validate`, `FhirClient.validate_with_esb()`), where
  RIE-SourceCode's `RIE.FHIR.Server` → `RIE.FHIR.Validate` runs the
  validator on the ESB servers. No OAuth2 token is required there
  (`Server.cls` skips introspection for `$validate`); one is sent anyway
  if `ESB_CLIENT_ID`/`ESB_CLIENT_SECRET` are set. **The ESB chooses
  profile, `-bundle` rule, terminology server and ignored issues** (its
  FHIR.Validate settings) — the page hides those selects and labels each
  result with what the ESB is expected to use
  (`fhir_validator.esb_validation_plan()`, from FHIR.Validate's
  defaults). It only accepts message Bundles (O21/O51/R01/R32) and single
  non-Bundle resources (NW profile named after the type); anything else
  comes back as an error issue. Its "FHIR Validation disabled" reply is
  shown as an error. The ESB may blank `ServiceRequest.code.coding.display`
  before validating, so line numbers can drift for those.
- **`local`** — the original in-app path, kept to revert to:
  the HL7 FHIR Validator CLI (`fhir_validator.run_validator()`,
  `validator_cli.jar` as a subprocess — same tool/flags as the IG's
  https://nw-gmsa.github.io/en/testing-resources.html). Everything below
  about profile choices, IG packages, `-bundle` rules and terminology
  servers applies to this backend only.

For a Bundle, the whole Bundle
and/or each entry (validated on its own, extracted from the Bundle) can
be ticked, each with its own profile `<select>` built from the
StructureDefinitions in two IG packages: NW GMSA
(`https://nw-gmsa.github.io/package.tgz` — **not** `/en/package.tgz`,
which the testing page's relative link suggests but 404s; re-downloaded
after 24h since the IG is rebuilt often) and HL7 Genomics Reporting 3.0.0
(packages.fhir.org). Defaults: the NW profile in `meta.profile`, else the
NW profile named after the type (`BundleMessage`/`BundleDocument` for a
Bundle). For a message Bundle's "Entire Bundle" row, an extra select adds the
IG testing page's validator `-bundle` rule (`fhir_validator.BUNDLE_RULES`):
O21 → `-bundle ServiceRequest:0 .../ServiceRequest`; R01 →
`-bundle DiagnosticReport:0 .../LaboratoryReport` (default) or
`.../TestResults` — defaulted to the first rule for the
`MessageHeader.eventCoding.code`; a rule forces the NW package to be
loaded (it depends on Genomics Reporting, so either profile resolves).
Issues are labelled per entry (`describe_resource()`: type, id,
code/value text) and the JSON is validated indented (`pretty()`) so
reported line numbers are real; clicking an issue's location opens that
exact JSON with the line highlighted.
**Page actions**: a FHIR message Bundle gets **Submit to
$process-message**, **Validate** and **Format JSON**; any other FHIR
resource just **Validate** and **Format JSON** (one button row under the
editor). With the local backend the profile / `-bundle` / terminology
selects sit in a collapsed "Validation options" `<details>`; with the ESB
backend there are none.
**The results are a "Data security report"** (aimed at eventually being
an onboarding step — `fhir_validator.build_report()`, shared by the page
and `/testing/send-message/report.pdf` —
`pdf_report.validation_report_pdf_bytes()`, same `?info=` query string
so they match): errors and warnings only unless "Include information" is
ticked. **Ignored issues are the ESB's job** (FHIR.Validate's
`IgnoredIssues`), so the old "Ignore" box is gone; `DEFAULT_IGNORED_ISSUES`
is still applied silently (a no-op on the ESB's output, still needed for
the local validator's) and a count is shown only if something was left
out. **Overall result** (`_overall_result()`, a banner at the top of the
page and PDF): any error → **Fail**; a resource that couldn't be validated
at all (and no errors) → **Incomplete**; warnings only → **Pass with
warnings**; else **Pass**. Below it, **"Tackling the issues"**
(`_advice()`) — numbered advice built from what the report found: errors
before warnings, N errors from D distinct problems (fix once, fixes every
occurrence), "Unable to find a profile match" errors are symptoms whose
reasons are in the automatically validated resources, the one Observation
standing in for the others (`represents`), and re-validate after each
round. **Validate runs in two automatic stages** (one background job): the
whole resource/Bundle, then — for a Bundle — every entry named in an
**"Unable to find a profile match for <ref> among choices: <profiles>"**
issue (which only says a reference target failed its targetProfile, not
why) is validated on its own against its type's default profile
(`fhir_validator.follow_up_targets()` → `default_choice()`; the NW
`Observation` profile for an Observation). **Only the first such
Observation** (`FOLLOW_UP_FIRST_ONLY_TYPES`) — a message usually carries
many Observations built the same way, so they share one problem. The validated
Observation's section lists the others it stands in for (`represents`,
from `follow_up_targets()`). These
sections are marked "Validated automatically" (`auto`) on the page and in
the PDF. `annotate_profile_mismatches()` finds `<ref>` in the uploaded
Bundle (fullUrl, Type/id, or `Bundle.entry[n]`).
**The manual "Validate individual resources" panel and the per-issue
"Validate <entry> against <profile>" buttons are hidden, not deleted** —
`app.INDIVIDUAL_VALIDATION_ENABLED = False`; set it to `True` to bring
them back. That path (`action=validate_entries`, its own
`<form id="entries-form">` whose submit handler copies the editor's
current text into a hidden `message` field) is still wired up: each run
is a new job carrying earlier results over (`validated: True`, not
re-run), and carried-over results whose resource has changed since
(`fhir_validator.canonical()`) are flagged `stale`. Identical issues are
grouped into one row (`_group_issues()`: same severity, message and
location once `Bundle.entry[n].resource` is replaced by the entry's
type) listing every entry it occurs in. The message regex (`_PROFILE_MATCH_RE`) follows the
validator's message text, not yet checked against a real run.
Terminology server: tx.fhir.org (`-sct uk`), none (`-tx n/a`),
or `FHIR_VALIDATOR_TX_URL`. One JVM run per distinct (IG, profile) — each
is tens of seconds — so it runs as a background job (`_validation_jobs`,
in-memory, owner-session only, same no-expiry caveat as
`_session_clients`) and `?validation=<id>` refreshes itself until done.
Needs Java 17+ and `data/validator/validator_cli.jar` (git-ignored;
`FHIR_VALIDATOR_JAR`/`FHIR_VALIDATOR_JAVA`/`FHIR_VALIDATOR_JAVA_OPTS`/
`FHIR_VALIDATOR_TIMEOUT` — see the module docstring); the page says what's
missing instead of failing. The validator itself fetches the packages'
dependencies (UK Core, EU Lab, ...) from the package registry, so it
needs outbound internet on first run.

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

**`ServiceRequest.requester` isn't always a PractitionerRole on this
server, and a PractitionerRole's `.practitioner` isn't always a
resolvable reference either** — two real cases hit on the same server:

- `ServiceRequest/25137`'s `requester` references a `Practitioner`
  directly (FHIR R4 allows this; the IG's own examples just happen to
  use PractitionerRole). Before the fix, this fell into
  `requester_display()`'s "unexpected resource type" branch (raw display
  text/id, not the resolved name) and into
  `requesting_clinician_display()`'s "not a PractitionerRole" branch
  (silently "—"). Both methods now check `resourceType == "Practitioner"`
  explicitly before falling through to the PractitionerRole branch.
- A different order's requester PractitionerRole has a `.practitioner`
  that's a **logical reference** — `display` + `identifier` (a GMC
  number), but no `.reference` to actually fetch. `resolve_reference()`
  only works off `.reference`, so this silently resolved to nothing
  before the fix. `FhirClient._resolve_practitioner_display()` now tries
  `resolve_reference()` first and, when that comes back empty, falls
  back to the practitioner reference's own inline `display` +
  `_reference_identifier_code()` (a GMC/GMP check against
  `Reference.identifier`, the logical-reference equivalent of
  `_practitioner_registration_code()`'s check against a resolved
  resource's `identifier[]`).

`_practitioner_display_name()` (resolved-resource case) and
`_resolve_practitioner_display()` (adds the logical-reference fallback)
are the shared formatters both `requester_display()` and
`requesting_clinician_display()` go through for a PractitionerRole's
practitioner — extend those, not either caller, if another requester
shape turns up.

`requester_display()` renders as "Dr X (GMC 1234567) (Org Y)" —
`FhirClient._practitioner_registration_code()` appends the requesting
clinician's GMC or GMP registration number (see item 12 under "Things
that are unverified" below) in brackets right after their name, before
the organisation, whenever the underlying Practitioner resource carries
one. This is still the shared implementation behind work orders/test
orders' "Requested by" column and the `order_view`/`report_view`
"Requesting organisation" field — it deliberately falls back to the
*organisation* name when the requester references an Organization
directly (no individual clinician involved) or a PractitionerRole has
no linked Practitioner, since those fields are about "who/what to
attribute this order to" generally.

`requesting_clinician_display(order)` is a separate, narrower method for
the "Requesting clinician" columns/fields added to `/ctdna`, the
`/stats` organisation/ICS drill-downs, `/order/<id>` (a new row above
"Requesting organisation"), and the patient page's "Genomic test
orders" table (a new column, `order_clinician` in `app.patient_detail()`,
between "Ordered" and "Requesting organisation") — it returns "—" in
exactly the cases `requester_display()` falls back to an organisation
name, rather than showing that organisation name, since a
clinician-specific column/field showing an organisation name would be a
wrong answer, not just a less specific one. Both share
`_practitioner_registration_code()` for the GMC/GMP suffix, so it
appears consistently wherever a clinician's name is shown either way.
Work orders/test orders (`/work-orders`, `/test-orders`) still only have
the combined "Requested by" column — not extended to a separate
clinician column, since that wasn't asked for there.

**The patient page's orders table went further still**: its old
"Requested by" column (`requester_display()`, mixing clinician + org)
was replaced with a **"Requesting organisation"** column showing just
the organisation name and its NHS ODS code in brackets — `"Name (ODS)"`,
the same `_org_display_name()` format (and same underlying
`order_organisation()`/`order_organisation_ods()` lookups) `/ctdna`
already used for its per-organisation section headings — now that
"Requesting clinician" is its own column, "Requesting organisation"
doesn't need to also carry the clinician's name. `app.patient_detail()`
builds this as `order_organisation` (renamed from the old
`order_requester` dict — do not confuse with the *function*
`client.order_organisation()` it calls per order, or the
same-named-but-unrelated `order_organisation` dict `_order_worklist()`
builds in `app.py` for the org filter dropdown on work/test orders).
Shows "—" when the order has no resolvable requesting organisation at
all (not just no ODS code).

### Daily stats (`/stats`)

Queries `ServiceRequest`/`DiagnosticReport` system-wide for a date range
(default: last 7 days) via `authored`/`date` search params, using
`_search_all_split()` to follow `Bundle.link[rel=next]` pages, capped at
`app.STATS_MAX_PAGES` = 100 pages per resource type (10,000 records at
`_count=100`; raised from 10). `app._stats_fetch()` does both searches for
`/stats`, `/stats/organisation` and `/stats/ics`, and each page shows a
"may be incomplete — narrow the date range" warning when either search
stopped at the cap with pages left (`FhirClient.last_search_truncated`,
not a result-count check — see the monthly report section). Breaks results
down by day, organisation, indication, ICS, and country.

- **`orders_in_range()` bundles only patient; `reports_in_range()` only
  patient/performer — one hop each, deliberately no requester chain and
  no ServiceRequest.** `orders_in_range()` used to `_include` the
  requester + `_include:iterate` PractitionerRole's
  Practitioner/Organization; the requesting trust now comes off the order
  itself (`app._order_ordering()` → `order_ordering_organisation()`:
  `requester.identifier` ODS — where the IG's own ServiceRequest examples
  put it, even for a PractitionerRole requester — else encounter/placer
  number assigner), falling back to resolving the requester reference
  (one cached GET per distinct requester) only when the order carries
  no ODS code at all. Both
  seed the reference cache (`_cache_included()`). `reports_in_range()`
  used to also `_include=DiagnosticReport:based-on` + the order's
  requester chain, which **413'd on production** (a month of reports;
  `_count` backoff to 5 didn't help) — so nothing on `/stats`,
  `/stats/organisation`, `/stats/ics` or `/stats/monthly-reports` reads a
  report's ServiceRequest any more. A report is paired with an order only
  if that order is already in `orders_in_range()`'s result
  (`FhirClient.index_orders()` + `match_report_order()` — by `basedOn`'s
  literal reference, else its logical identifier; never a fetch), and its
  ordering organisation comes off the report itself (`app._report_ordering()`
  → `report_ordering_organisation()`; see "Monthly reports" below). This used to be a
  plain `_search_all()` with no `_include` at all — `app.stats()` calls
  `patient_for()`/`order_organisation_resource()`/`report_organisation()`/
  `order_for_report()` once per row, so every one of those was a separate
  uncached `resolve_reference()` GET: an N+1 that scaled with order/report
  count (not distinct-entity count, since patients rarely repeat within a
  week) and was slow enough to time the page out on a real week of data.
- Order indication comes from `ServiceRequest.reasonCode`.
- Reports don't carry their own indication: `report_indication(report,
  order)` uses the matched order's `reasonCode` when the order is in range,
  else the indication encoded in the report's own Test Directory code
  (`"R240"` from `R240.1`, plus its description), else `conclusionCode` —
  it never fetches the order.
- Stats still resolves one `Patient` per order/report for ICS/country, but
  the `_include` above means that's now a cache hit rather than a GET for
  every patient the paginated queries actually returned — the only way it
  still falls back to per-patient GETs is a server that doesn't honour
  `_include` at all (see the `_include`/`_include:iterate` support caveat
  below), the same graceful-degradation case `lab_orders_for_patient()`
  already handles.
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
  - "Ordering provider" is who *ordered* the test — read off the report
    itself (`app._report_ordering()`: `report_ordering_organisation()`,
    falling back to the in-range matched order's
    `order_organisation_resource()`), not its ServiceRequest (see the
    413 note above). The map uses `organisations_geocoded()` — the
    grouping/geocoding `orders_by_organisation_geocoded()` now wraps —
    over each report's (organisation, name) pair, so counts land
    per-report. `report_rows` grew an
    `ordering_provider` field (alongside the pre-existing `organisation`
    field, which stays the *performing* org) so `report_by_ordering_provider`
    can use the same `group_count()` table pattern as everywhere else.
  - The ICS choropleth is `ics_choropleth_html()` called on
    `group_count(report_rows, "ics")` exactly as the orders side calls it
    on `order_rows` — no report-specific logic needed, since ICS comes from
    the patient either way.
  - Map div IDs are namespaced (`org-map` for orders, `report-org-map` for
    reports) since both Leaflet maps render on the same page.

### Monthly reports (`/stats/monthly-reports`)

**Coverage: iGene and Shire reports only — not StarLIMS** (stated on the
page itself too). Reports per calendar month (default: just the last full calendar month,
so the default page load is a single month's search; the current month can
be picked too, counted up to today) by ICS and report category — "Monthly reports" under
Management Reports. Queried **one month at a time** (`reports_in_range(...,
max_pages=MONTHLY_REPORTS_MAX_PAGES)`), so each search stays bounded; a
month that hits the page cap is flagged on the page as possibly incomplete.

- **No `_include` on the report search** (`reports_in_range(...,
  with_includes=False)`) — the original `_include`d
  `DiagnosticReport:based-on` + the requester chain and 413'd on
  production for a single month. **Searched in week-sized windows**
  (`MONTHLY_REPORTS_WINDOW_DAYS` = 7, each with its own
  `MONTHLY_REPORTS_MAX_PAGES` = 100 page cap, i.e. 10,000 reports/week at
  `_count=100`) — one 20-page search per month hit the cap (2,000) on
  production for October 2026. The "may be incomplete" warning comes from
  `FhirClient.last_search_truncated` (set by `_search_all_split()` when it
  stops with a next page left), not from the result count — `_count` can
  be halved by the 413 backoff, so a count check would miss it. The ordering trust's ODS code
  is read off the report itself where it has one (`report_ordering_ods()`:
  `encounter.identifier.assigner`, else the PLAC-typed
  `basedOn.identifier.assigner`, skipping the report's own performer —
  the GLH, e.g. `699X0`); **on production most reports don't**, so for
  the rest their ServiceRequests are fetched in batches
  (`app._orders_index_for_reports()` → `FhirClient.service_requests_for_reports()`:
  `ServiceRequest?_id=a,b,c,...` / `identifier=...`, 50 per search, no
  `_include`, seeded into the reference cache) and the code read off the
  order (`order_ordering_ods()`: `requester.identifier`). Then `organisation_by_ods()` names it (ODS's
  trust list for an NHS trust, else this server's Organization topped up
  from the NHS ODS API), and the ICS/region come from ODS's trust → ICB
  mapping (`FhirClient.trust_geography()`, see "ICS from the ODS trust →
  ICB mapping" below). **Which of
  those assigners this server actually populates is unconfirmed** — if
  everything lands in "Unknown", sample a real report's `encounter`/
  `basedOn`. Each ICS row expands (client-side, hidden by default) into
  its trusts.
- **One row per trust** — `app._canonical_org()` swaps whatever
  Organization a report/order resolved to for `organisation_by_ods()` on
  its ODS code, and `_org_name()` title-cases an all-capitals name.
  Without this the same trust appeared twice (same ODS code, "MANCHESTER
  UNIVERSITY NHS FOUNDATION TRUST" from this server's own Organization via
  a resolved requester reference, and the ODS trust list's mixed-case
  name via an ODS code on the order), since rows group by name. Used by
  `_order_ordering()`/`_report_ordering()`, so daily stats gets it too.
- **"Reports actually delivered"** — reports whose ordering trust
  receives them electronically: counts and % of the total per status (Live
  / Nearing go-live / Not live), then the **top 10 recommended target
  trusts** by volume that are neither live nor nearing go-live (reports
  and % of total each).
  Live status is **maintained by hand** in `app.TRUST_DELIVERY_STATUS`
  (by ODS code: `R0A` MFT live; `RBS` Alder Hey and `REP` Liverpool
  Women's nearing go-live as of 2026-11) — update it as trusts go live.
  Uses the same ICS list (region filter) and counting (category filter) as
  the ICS table, so totals match. `_delivery_summary()` in `app.py`; also
  in the PDF.
- **iGene IGEAP codes** (`https://fhir.nwgenomics.nhs.uk/CodeSystem/IGEAP`)
  are what most production reports/orders carry — often *only* that, with
  no England Test Directory code in `DiagnosticReport.code`, which they
  should have (a data-feed gap). **Precedence in `report_category()`:**
  any Test Directory / NW local / Histotrac code on the report *or* its
  order first, then mapped LOINC codes, then IGEAP last — so an order's
  Test Directory code beats an IGEAP-only report. Failing those, `FhirClient._categorise_igeap()` reads
  the IGEAP code/display: "DNA Storage Only"/"Not Processed" → new
  `no_test` category; an embedded Test Directory code ("PANEL: R208.1 -
  ...", "R27.3_WGS") → categorised as that code; an indication in the
  panel code ("M4_LUNG_GX", "v2bM215_...", "CCP19v1_R131v5") → as an M/R
  code; then technique rules (e.g. "SNP Microarray" → cytogenomic),
  haem-onc/solid keywords (BCR::ABL1, NPM1 MRD, PanHaem, fusion) →
  somatic, Sanger predictive/family follow-up → variant. Checked against
  the top 25 production unclassified IGEAP codes (all categorised); the
  IG only publishes a 3-code fragment of IGEAP and no mapping, so these
  rules are inferred.
- **"Unclassified reports — what codes do they carry?"** — a collapsed
  section under "Reports by category" listing the most common (report
  code, order code) combinations among unclassified reports
  (`_code_signature()`, top `UNCLASSIFIED_SIGNATURES_SHOWN`), region filter
  applied. Every code in the IG's GenomicTestCode CodeSystem (2,130 R/M
  codes) is categorised, so anything here is a code from another system
  (SNOMED "Genetic report", iGene local codes, other LOINC, text only, ...)
  — map it in `report_category()`/`LOINC_REPORT_CATEGORIES`. Not in the PDF.
- **A report's ServiceRequest is fetched when it lacks an ODS code *or* a
  Test Directory / NW local / Histotrac code of its own**
  (`_orders_index_for_reports()`) — originally only the former, so a
  report coded only as SNOMED "Genetic report" but carrying its own ODS
  code never got its order's R/M code and stayed Unclassified. It's "lacks
  a Test Directory code", not "can't be categorised", because an
  IGEAP-only report *can* be categorised from IGEAP, but its order's Test
  Directory code takes precedence.
- **"Download as PDF"** (`/stats/monthly-reports/pdf`, same query string
  as the page) — `pdf_report.monthly_report_pdf_bytes()`, reportlab,
  landscape A4, same pattern as `/data-quality/pdf`. Both routes build
  their data via `app._build_monthly_report()` (the page's template
  context), so they can't disagree; `_monthly_report_context()` caches each
  login's last result per query string for `MONTHLY_REPORT_CACHE_SECONDS`
  (10 min) so downloading straight after viewing doesn't re-run every
  search. The PDF writes out what the page hides behind clicks: every
  ICS's trusts are listed (indented) under it, and a final "Categories by
  ICS" section gives each ICS's category breakdown (the popup), each kept
  on one page.
- **"Categories" popup** — a button on every ICS and trust row opens a
  native `<dialog>` (`#category-dialog`, `monthly_reports.html`) with that
  row's reports by category × month, plus totals. Built from counts the
  route already has (`category_breakdown()` in `app.monthly_reports()`,
  embedded per button as `data-breakdown` via `tojson`) — no extra
  searches. Always all categories, regardless of the category filter;
  categories with no reports are left out.
- **NHS region filter** — regions are `FhirClient.NHS_REGION_NAMES`
  (the 7 ODS `Yxx` codes); the ICSs in each come from
  `FhirClient.icbs_by_region()` (every open trust's ICB in ODS's trust
  list), not a hardcoded list — ICBs merge (six new ones on 2025-11-13:
  West and North London, Thames Valley, Surrey and Sussex, Essex, Central
  East, Norfolk and Suffolk; 36 ICBs after). Picking a region lists
  *every* current ICS in it, zero-filled.
- **Categories have sub-categories** (`FhirClient.REPORT_CATEGORY_TREE`):
  Somatic Cancer → Haemato-oncology / Solid tumour (M indication in
  `_HAEM_ONC_INDICATIONS` = M80–M117 + M181/M182/M224/M225/M235, else
  wording for MX/HX/IGEAP); Cytogenomic → Prenatal (fetal/prenatal/
  miscarriage/NIPT wording) / Postnatal / Cancer (M codes); Variant
  reporting → Rare disease / Inherited cancer (R indication in
  `_INHERITED_CANCER_INDICATIONS`, else "cancer"/BRCA/Lynch wording); plus
  new top-level **MRD monitoring** (M codes with "MRD" in the name, iGene
  BCR::ABL1/NPM1 quantitative RT-PCR) and **ctDNA**. `report_category()`
  returns a *leaf* key; parent rows are their children's sums, and only
  top-level rows go into totals (page, popup JS and PDF all follow this).
  The category filter accepts a parent (sums its leaves via
  `category_leaf_keys()`) or a sub-category.
- **Categories are inferred from the test code** —
  `FhirClient.report_category()` (the report's own `code`),
  rules documented in its docstring: H&I (Histotrac codes, M118/R428/M242,
  HLA/chimerism) → pharmacogenomics (DPYD/TPMT/NUDT15/CYP2xx, R454, RX06/
  RX07) → ctDNA (ctDNA/cfDNA in the display: M3.13, M4.5, M4.14, M226.7,
  MX13.1, MX14.1 — its own category, out of Somatic Cancer) → WGS (M/MX/HX codes as "Cancer Whole Genomic Sequencing", the
  rest as "Whole Genomic Sequencing") → cytogenomic (karyotype/microarray/FISH/aneuploidy, germline
  *or* cancer) → remaining M/MX/HX = somatic cancer → remaining R/RX =
  variant reporting → else unclassified. A CodeableConcept with no
  recognised test code (on the report or its order) can still match a
  LOINC code in `FhirClient.LOINC_REPORT_CATEGORIES` (`33893-9`, bone
  marrow karyotype → cytogenomic), then an IGEAP code — add more there as
  they turn up in "Unclassified". Not a published mapping — adjust
  the rules there if a category looks wrong.

### ICS from the ODS trust → ICB mapping

Every "by ICS" breakdown (`/stats` tables, choropleths and `/stats/ics`
drill-down, `/stats/monthly-reports`) gets an organisation's ICS from
**NHS ODS's own trust → ICB mapping**, by ODS code — not from its
postcode. **The data is bundled in the repo** — `data/ods/etr.csv`
(ODS's NHS Trusts export; columns: code, name, region `Yxx`, ICB code,
..., postcode, close date) and `data/ods/icb_names.json` (ICB code →
name), written by `scripts/update_ods_reference_data.py` (re-run and
commit when trusts/ICBs change) — so the app needs **no outbound internet
access** for this; `FhirClient.ods_trusts()`/`icb_name()` only download
(`ODS_TRUSTS_REPORT_URL` / the ODS lookup API) if a file is missing, and
after a failed download wait `ODS_RETRY_AFTER_SECONDS` before retrying
rather than retrying per report (the likely reason a production run put
R0A under "Unknown" — not confirmed, but a failed download there left
every trust without an ICS and was retried for every report). `trust_geography(ods)` returns ICB code/name and region;
`organisation_by_ods()` names a trust from the same file. A 5-character
site code ("R0A07") counts under its 3-character parent trust ("R0A") in
both. `organisation_ics(org)` is just
`trust_geography(organisation_ods_code(org))`.

- This **replaced** postcode → postcodes.io → point-in-polygon against
  the ONS ICB boundaries (`icb_for_coordinates()`, removed). postcodes.io
  is now only used for the `/stats` organisation **map markers**
  (`organisation_geocode()`); the ONS boundaries only for drawing the
  choropleths (`fetch_icb_boundaries()`).
- **Anything that isn't an English NHS trust goes through
  `FhirClient.organisation_geography(ods, name, postcode)`** (used by
  `organisation_ics()` and the monthly report) — previously all of these
  were "Unknown" ICS. In order: `trust_geography()`; then the NHS ODS
  API's own record for the code (`ods_details()`, cached per code):
  primary role ICB (RO261) → that ICB, commissioning region (RO209,
  e.g. Y62) → "North West (ICS not known)", independent sector (RO172,
  e.g. Spire NT3) → "Independent sector" (its ODS link is to its head
  office's ICB, so deliberately not used), `Country` Wales/Scotland/
  Northern Ireland/Isle of Man/... → that country, a relationship to an
  NHS trust (699X0 GLH → R0A) or an ICB (sub-ICB location 01G → QOP) →
  that trust's/ICB's ICS; then the postcode area (`POSTCODE_AREA_GROUPS`,
  border-straddling areas left out); then the code/name
  (`NAME_GEOGRAPHY_RULES` — local codes ODS doesn't know, e.g.
  "SS999 NHS Lothian", "ABUHB_Wales", "IRL Ireland", GENQA/UK NEQAS →
  "External quality assessment", ISO country codes/names → "Other
  countries"); finally an ODS-known English organisation with nothing
  else (closed trust RFZ) → "England (ICS not known)". Countries outside
  England get region `OUTSIDE_ENGLAND_REGION` ("Outside England"), which
  the monthly report's region filter offers too. Still "Unknown": no
  ordering organisation at all, or a local code with an uninformative
  name (`SH_OLDHAM`, `NOTKNN`).
- The ONS boundary set is **April 2023**, so the November 2025 merged
  ICBs have no polygon: they're counted in the tables but listed as
  unmatched under each choropleth.
- A failed `etr.csv` download isn't cached (retried next request); the
  monthly report shows an error banner and only ICSs with data.

### ctDNA summary (`/ctdna`)

A cross-patient turnaround-time view for **ctDNA** (circulating tumour DNA)
genomic test orders: order date, sample collection date, sample received
date, date reported, and conclusion code, one row per order.

- **ctDNA detection is code-based where confirmed, text-based as a
  fallback**: `FhirClient._is_ctdna_order()` first checks the order's
  Genomic Test Directory code (`test_directory_code()`) against
  `CTDNA_TEST_DIRECTORY_CODES` (confirmed: `M4.14`), then falls back to a
  text match on `ServiceRequest.code`'s text against `CTDNA_TEXT_MATCHES`
  ("ctdna", "circulating tumour/tumor dna", "cfdna", etc.) for orders that
  don't carry that code — either an older/different code, or a server
  that doesn't populate `GENOMIC_TEST_DIRECTORY_SYSTEM` consistently. Add
  more codes to `CTDNA_TEST_DIRECTORY_CODES` as they're confirmed.
  `ctdna_orders()`'s outstanding and completed-without-report buckets each
  also run a **supplementary `code=` FHIR search** (see
  `_ctdna_code_search_value()`) alongside their existing `category`-based
  query, pooled together — not a replacement, since `category` should
  already be a superset covering these, but a server that doesn't
  populate `category` reliably would otherwise miss a genuinely
  ctDNA-coded order entirely.
- **`FhirClient.ctdna_orders(start, end)`** used to be a single unbounded
  system-wide `ServiceRequest` query — fine until a server with a large
  ctDNA history started 413ing, since it pulled back *every* ctDNA order
  ever (plus each one's `_include`/`_revinclude` fan-out) on every page
  load regardless of what range was picked. It's now three separate
  `_search_all_split()` queries (the split-aware sibling of `_search_all()`
  — both share pagination logic, each capped at the same 1,000-record
  default), pooled together before filtering:
  1. **Outstanding** — `status` anything but `completed`
     (`NON_COMPLETED_STATUSES`), no date bound, same as before.
  2. **Completed with a report** — queried from the *DiagnosticReport*
     side, bound by its own `date`/`issued` to `[start, end]`, then
     `_include=DiagnosticReport:based-on` pulls back the originating
     ServiceRequest. Deliberately not a `ServiceRequest.authored` bound
     here — `app.ctdna_summary()`'s `completion_date` prefers the report's
     `issued` date, so an order placed well before the window but
     completed inside it would be wrongly dropped if the query bound the
     order's own `authored` instead.
  3. **Completed with no report at all** — the one case query 2 can't
     reach, so bound by the ServiceRequest's own `authored` instead,
     matching `completion_date`'s fallback.

  All three bundle each order's `specimen`/`patient`/`requester` via
  `_include` (query 2 reaches them one hop further via `_include:iterate`,
  since the primary match there is the DiagnosticReport, not the
  ServiceRequest), and any linked `DiagnosticReport` via
  `_revinclude=DiagnosticReport:based-on` on queries 1 and 3 (+
  `_include:iterate` for that report's own specimen and the
  Organization/Practitioner behind a PractitionerRole requester — reuses
  `SERVICE_REQUEST_ITERATE_INCLUDES`, the same constant the patient-page
  queries use). `start`/`end` are optional (`None` leaves the completed
  bucket unbounded, the old behaviour) but `app.ctdna_summary()` always
  passes both. Returns `(orders, reports_by_order_id)` — the latter maps
  an order's id to its most-recently-issued linked report, since a
  reflex/repeat test could produce more than one. **Both are built by
  filtering `resourceType` across every query's `matches + included`
  pooled together, not by trusting `Bundle.entry.search.mode`** — some
  servers don't reliably tag `search.mode` on `_include`/`_revinclude`'d
  entries, which would otherwise misfile a linked `DiagnosticReport` into
  `matches` (getting read as if it were an order, with none of the
  ServiceRequest's fields — empty order date/specimen data) and leave
  `reports_by_order_id` unable to find it at all. This was a real bug, not
  just a theoretical one — fix it the same way if a similar
  split-then-filter pattern shows up elsewhere. Duplicate resources across
  the three queries (e.g. a report reachable via both query 2 and its
  order's `_revinclude` in query 3) are harmless — both dicts key by
  resource id, so a repeat just overwrites itself with the same data.
- **The outstanding/completed split happens in `app.ctdna_summary()`**, not
  in `fhir_client.py`: "outstanding" is any `ServiceRequest.status` other
  than `completed`; "completed" orders are only included if their linked
  report's `issued` date (or the order's `authoredOn` if no report
  resolved) falls within `[start, end]` — a `start`/`end` date-range picker
  (same `?start=&end=` query-param shape as `/stats`), defaulting to the
  last 30 days when unset, and passed straight through to `ctdna_orders()`
  above so the FHIR query itself stays bounded, not just the post-fetch
  filtering. The outstanding bucket is also bound to `[start, end]` now
  (by `ctdna_orders()`, via `authored` — see above), so `ctdna_summary()`
  itself does no additional date filtering for it, just the status check.
  This used to be unconditionally unbounded, on the theory that an old
  still-active order is exactly the kind of thing this screen exists to
  surface — reverted after a live server with a large non-completed
  backlog hit a 413 on that query specifically.
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

### Data quality report (`/data-quality`)

The one screen in this app that isn't FHIR at all — it queries
`RIE.PatientDemographics`, a table in the InterSystems IRIS database
behind `ENTERPRISESERVICEBUS` (the FHIR server's own source data,
upstream of the `Patient` resources everything else in this app reads),
directly over SQL via `iris_client.py`'s `IrisClient`, not through
`FhirClient`/`client`. Reuses the logged-in user's own FHIR credentials
(`client.user`/`client.password`) rather than a second login — confirmed
to work against both the FHIR API and this IRIS database on this
deployment.

- **Connection config is env-var-only, no hardcoded deployment
  defaults** — `IRIS_HOST`/`IRIS_NAMESPACE` have no fallback and
  `IrisClient.__init__` raises a clear `ValueError` if either is unset
  (same pattern `FhirClient` uses for `FHIR_BASE_URL`); `IRIS_PORT`
  defaults to `1972` (IRIS's standard superserver port) since that's not
  deployment-specific. The driver import itself
  (`intersystems_iris.dbapi`) also has a fallback: the module path this
  was originally built against
  (`intersystems_iris.dbapi._DBAPI`) only exists on older driver
  releases — on the current `intersystems-irispython` package (pulled in
  via the `intersystems-iris` compatibility shim), `dbapi` is a plain
  *attribute* `intersystems_iris`'s `__init__.py` sets, not a real
  importable submodule, so `import intersystems_iris.dbapi` (as a
  statement) raises `ModuleNotFoundError` even though
  `intersystems_iris.dbapi` (attribute access) works fine. `iris_client.py`
  tries the old submodule path first, then falls back to plain `import
  intersystems_iris` + `getattr(..., "dbapi", None)`.
- **This table's actual columns aren't confirmed against a real
  server.** Most identifying columns (NHS number, MRN, surname,
  forename, DOB, postcode, source, score, last-updated) are located by
  best-effort case-insensitive name matching (`_find_column()` against
  pattern lists like `NHS_NUMBER_PATTERNS`/`SOURCE_PATTERNS`/
  `SCORE_PATTERNS`/`LAST_UPDATED_PATTERNS` etc. in `iris_client.py`) —
  same "detect, don't assume, degrade gracefully" convention
  `fhir_client.py` uses throughout. The seven columns behind *why* a
  score is low (see "Quality" below) were given exact names rather than
  guessed, so those are located by case-insensitive **exact** match
  instead (`_find_exact_column()` against `QUALITY_FLAG_COLUMNS`/
  `QUALITY_MATCH_COLUMNS`) — IRIS SQL commonly upper-cases unquoted
  identifiers, so this still doesn't assume one particular case. The
  whole quality section is wrapped by `_safe_check()`, so a wrong
  assumption about a column's type/contents shows as an error on that
  one section rather than blanking the whole report.
- **No column completeness table and no generic checks (NHS number
  checksum, duplicate patients, DOB sanity, postcode format) any
  more** — the report used to lead with those, but they were replaced
  entirely by the "Quality" section below, which is both more specific
  (grounded in this table's actual PDS trace outcome columns, not a
  generic regex/checksum guess) and more directly useful (organised
  around *why* a score is low, the actual question this report exists to
  answer) than a generic completeness/checksum pass ever was.
- **Date range, defaulting to the last 30 days** (`?start=&end=`, same
  convention as `/stats`/`/ctdna`) — `app.data_quality()` passes these
  straight to `IrisClient.build_report(start, end, ...)`, which detects a
  last-updated-like column by name (`LAST_UPDATED_PATTERNS`) and, if
  found, threads a `WHERE "<col>" >= ? AND "<col>" <= ?` condition
  through **every** query in the report (row count, and everything the
  quality section queries) via `_with_filter()` — not just a post-fetch
  filter. `end` is treated as inclusive of the whole day (` 23:59:59`
  appended) unless the column's `DATA_TYPE` is bare `DATE`. If no such
  column is found, the report runs unfiltered over the whole table and
  says so (`date_filtered: False` — `data_quality.html` shows a note
  rather than silently ignoring the picker). The filter state
  (`self._date_filter_conditions`/`self._date_filter_params`) is
  instance state set fresh at the start of each `build_report()` call —
  safe only because `app.py` constructs a brand new `IrisClient` per
  request rather than caching one per user the way `FhirClient` is.
- **"Quality"** (`IrisClient._check_quality()`) — the section this
  screen was actually built for: rows with a **low or null** score,
  broken down by source (an NHS ODS code) and by *why*. "Low or null" is
  deliberately `score_col <= threshold OR score_col IS NULL`
  (`?score_threshold=`, default `app.DEFAULT_SCORE_THRESHOLD` = 8), not
  just the threshold check alone — a record PDS (Personal Demographics
  Service) can't trace at all likely never gets a score computed for it,
  and `NULL <= threshold` is false in plain SQL, so without the explicit
  `OR ... IS NULL` branch exactly the records missing the identifier this
  app relies on most for patient matching (see "Patient matching" above)
  would silently vanish from the one report meant to surface them. Only
  runs at all if a source-like and score-like column are both found
  (`report["quality"]` is `None` otherwise).
  - **Reasons** — up to eight, each independently detected and
    independently checked per row (`reason_columns` in
    `_check_quality()`): "no NHS Number present" (derived from whichever
    NHS-number-like column was found — bad when null/empty, not one of
    the exact-named columns below), `NHSNumberNotFoundPDS` (a flag
    column — bad when *true*, via `_flag_true()`), and
    `birthDateMatch`/`familyMatch`/`genderMatch`/`givenMatch`/
    `postalCodeMatch` (match columns — bad when *not* true, including
    `NULL`, since a match that was never evaluated isn't a confirmed
    match). **If `NHSNumberNotFoundPDS` applies, it's the *only* reason
    shown for that row** — the individual match columns aren't
    meaningful reasons in their own right when PDS never found a record
    to match against in the first place, so listing them alongside "not
    found in PDS" would just be noise; this collapse happens per row
    right after `row_reasons` is built, before it feeds into either the
    entry's `reasons` list or the `reason_totals`/`reason_totals_by_source`
    tallies, so a "not found in PDS" row is counted once, under that one
    reason, everywhere. "No NHS number present" is unaffected by this —
    it can still appear alongside other reasons. `_flag_true()`'s
    bit/boolean parsing (`1`/`0`, `'Y'`/`'N'`,
    `True`/`False`, ...) is a best-effort guess, unconfirmed against a
    real server. A reason column not found on this table is simply
    skipped rather than erroring — `reason_columns_detected` in the
    result lists which ones actually were, so the page can say so.
  - **De-duplicated, and last-updated is deliberately not one of the
    displayed/compared columns** — this table can carry more than one
    row per patient (e.g. re-traced against PDS on a different day) that
    would otherwise be identical; with a last-updated timestamp in the
    mix those read as distinct rows, which showed up as duplicate-
    looking entries in the per-source listing. `display_columns` (built
    from whichever of NHS number/**MRN**/surname/forename/DOB/postcode/
    source/score were detected as columns — deliberately *not*
    last-updated, unlike an earlier version of this report) is combined
    with each row's computed `reasons` list into a dedup key
    (`(display column values..., reasons...)`); only the first row per
    distinct key, per source, survives. **Every count in the result is
    derived from this de-duplicated set** — `total_low_or_null`,
    `reason_totals` (overall), each source's own `reason_totals`, and
    each `entries_by_source` group's `count` — there's no separate
    "before de-dup" number shown anywhere, since a raw row count wasn't
    the thing this report needed to answer. This *did* mean giving up
    the previous design's separate uncapped `GROUP BY` count query (kept
    specifically so per-source totals stayed exact even when the
    row-level listing was capped) — de-duplication can only happen after
    fetching actual rows, so there's no way to get an exact count
    cheaply anymore; the whole result now shares one cap,
    `IrisClient.MAX_LOW_SCORE_ENTRIES` (2,000) rows fetched before
    de-duplication, and `truncated` says when that cap was hit.
  - **Two views of the same de-duplicated rows**: `reason_totals`
    (overall count per reason, across every source, for "why is our data
    bad" at a glance) and `entries_by_source` (one heading per source —
    same "one `<h2>`/table per group" pattern `/ctdna` uses for
    organisations — each with its own `reason_totals` sub-breakdown and
    its actual unique rows).
- **"Download as PDF"** (`/data-quality/pdf`) — the same report as a
  downloadable PDF, built by `pdf_report.py`'s
  `quality_report_pdf_bytes()` via `reportlab` (landscape A4;
  `reportlab` chosen specifically for having prebuilt Windows wheels —
  no C compiler/system libraries needed at deploy time, unlike e.g.
  WeasyPrint — see `docs/windows-iis-deployment.md`). `app.py` factors
  the date-range/threshold parsing (`_data_quality_params()`) and the
  actual `IrisClient.build_report()` call
  (`_build_data_quality_report()`) out of `data_quality()` so both the
  HTML route and this one call the exact same code for the exact same
  query params — the HTML page's "Download as PDF" link just carries the
  current `start`/`end`/`score_threshold` through as a query string, so
  the PDF always matches what's on screen. `quality_report_pdf_bytes()`
  mirrors every branch `data_quality.html` has (a caught exception, a
  report-level `"error"`, a quality section that itself failed, no
  quality section at all because no source/score column was found) —
  each one stops the PDF after explaining why, same as the HTML page.
  Patient data going into table cells is passed as **plain strings**,
  never wrapped in a reportlab `Paragraph` — reportlab only interprets
  its mini-XML markup (`<b>`, `&amp;`, ...) for `Paragraph`/similar
  Flowable cell content, not plain strings, so a name or postcode
  containing `&`/`<`/`>` can't corrupt or crash the PDF. The handful of
  places that *do* build `Paragraph` text from data (a detected column
  name, an error message) run it through `xml.sax.saxutils.escape()`
  first for the same reason, since unescaped markup there could raise
  partway through the build.
  - **The Reasons column is the one exception to "plain strings, no
    wrapping"** — `_reasons_cell()` renders a row's reasons as a
    `Paragraph` (one reason per line, via `<br/>`, escaped like the
    other `Paragraph` text above) instead of a single comma-joined
    plain string. A plain string doesn't wrap inside a `Table` cell at
    all, so a row with several reasons produced one very long line that
    ran past its column — and past the whole table, since the entries
    table had no explicit column widths at that point, so reportlab
    sized every column to fit its widest *unwrapped* content. Fixed
    together with `_entries_table()`, which (unlike the plain `_table()`
    helper used for the two-column summary tables) always passes
    explicit `colWidths`: a fixed width per identifying column, with
    whatever's left of `_CONTENT_WIDTH` (the landscape-A4 page width
    minus both margins) going to Reasons — the one column that actually
    needs room to wrap into.

### Work orders owner (Organization or Device) — ServiceRequest-based

`/work-orders` is back on **ServiceRequest** (`intent=filler-order`,
`FhirClient.active_filler_orders()`), after a spell on Task
(`active_filler_tasks()`, still in `fhir_client.py` but no longer called)
ran into problems on this deployment. It's bounded by `authored`
(`start`/`end`, default last 30 days), filtered by
`ServiceRequest.status` (`SERVICE_REQUEST_STATUS_VALUES`, default
`active`), and scoped by **`ServiceRequest.performer`** — the
ServiceRequest counterpart of `Task.owner` — picked from
`FhirClient.TASK_OWNER_OPTIONS`: **K1S6S** (Liverpool GLH Starlims, an
Organization by ODS code, the default), **DLIMS** (Omics DSS, a `Device`
found by `Device?identifier=https://fhir.nwgenomics.nhs.uk/Id/laboratory-information-system|DLIMS`),
or "Any performer" (`?owner=` empty — no performer filter, the date
bound alone keeps it scoped). `resolve_task_owner()` does the lookup (any
other `?owner=` value is still treated as an ODS code). **Unconfirmed
whether this server populates `ServiceRequest.performer` on filler
orders** — if a known owner shows nothing, try "Any performer".
`performer_display()`/`_actor_reference_display()` render a Device via
`device_display()` (deviceName, else identifier). Order Placer Group
Number comes from `ServiceRequest.requisition`.

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
`/work-orders` and `/test-orders`: it calls `fetch_orders()` (a zero-arg
closure the route builds — see date range below), then builds the
per-order requester/patient lookups and the `build_order_chains()` tree —
the two routes just plug in a different closure and template.

Both screens deliberately reuse the patient page's "Genomic test orders"
table shape — same columns (Test/Status/Intent/Ordered/Requested
by/Placer ID/Filler ID/Reason Code Ref#/ID) and the same
`render_order_chain` macro for `basedOn` nesting — plus a Patient column
(`order_patient` dict, built the same way `order_requester` already is)
since these span multiple patients rather than being scoped to one.

`_active_orders_with_intent(intent, start, end)` mirrors `ctdna_orders()`'s
query shape: `_include`s `specimen`/`patient`/`requester` (plus
`_include:iterate` for the Practitioner/Organization behind a
PractitionerRole requester), and — same lesson as the real bug fixed in
`ctdna_orders()` — filters by `resourceType` across `matches + included`
combined rather than trusting `Bundle.entry.search.mode`.

**`start`/`end` (bounding `ServiceRequest.authored`) are required
parameters here, not optional like `ctdna_orders()`'s** — this query used
to have no date bound at all (only `status=active` + `intent`), which
turned out to be exactly the same unbounded-result-set 413 risk
`ctdna_orders()` was rewritten to avoid, just triggered by a large active-
order backlog instead of a large ctDNA history. `work_orders()`/
`test_orders()` both read `start`/`end` query params (same `?start=&end=`
convention as `/stats`/`/ctdna`) and close over them in the `fetch_orders`
lambda passed to `_order_worklist` — `work_orders()` defaults to the last
30 days, `test_orders()` to the last 7 (same as `/stats`'s default): a
30-day range of placer-side orders was still enough to 413 on a live
server, so it was tightened after the fact for this screen specifically.

**Both screens also have an organisation/test filter and a sortable
Ordered column**, applied client-side after the date-bounded fetch (not
separate FHIR queries): `order_organisation()`/`test_directory_code()` are
computed per order, `organisations`/`tests` are the distinct sorted values
for the two "All or specific value" `<select>`s
(`app._filter_orders_by_org_and_test()`, shared by both routes), and
`app._sort_order_chains()` sorts `build_order_chains()`'s node list by
`authoredOn` (recursing into children too) when the "Ordered" column
header link is clicked — it toggles `?sort=ordered_asc`/`ordered_desc` via
`url_for`, carrying the current date range/org/test selection forward. No
splitting by organisation into separate sections the way `/ctdna` does
(just a filter down to one at a time).

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
  fhir_client call needed for the count); the delete route re-fetches via
  `client.active_placer_orders(start, end)` and reapplies
  `_filter_orders_by_org_and_test()` — both passed through as hidden form
  fields — before calling `FhirClient.orders_with_unknown_patient(orders)`
  / `clear_down_orders_with_unknown_patient(orders)`, which filter on
  `patient_for(order) is None` — broader than `orphaned_service_requests()`
  (wholly-absent `subject` only), since a present-but-dangling reference
  counts too. Scoping the re-fetch to the same date/org/test filter the
  page was showing matters here — otherwise the displayed "N of the
  orders above" count could disagree with how many this actually deletes.
  Both delete methods for ServiceRequests
  (`clear_down_orphaned_service_requests()` and this one) now share a
  `_delete_service_requests(orders)` helper. Single POST, no separate
  confirm route, same reasoning as the admin screen's orphaned-SR delete:
  no patient identity involved, and the "Unknown" cells are already
  visible on the page before the button is reachable.
  `admin_clear_down_result.html` (the shared result template) now takes
  optional `back_url`/`back_label` params so this route's result page
  links back to `/test-orders` (with the same date/org/test query string)
  instead of `/admin`.

No splitting by organisation into separate sections the way `/ctdna`
does — flagged in README as an obvious next step if either screen needs
it.

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

**On production (`client.is_production()`) it's enabled but
password-gated** — it used to be blocked outright there. The confirm
form adds a password field, and the POST is rejected (403, nothing
deleted) unless it matches `app.CLEAR_DOWN_PASSWORD` (currently
hardcoded, `secrets.compare_digest` comparison). Non-production servers
need no password, same as before.

This route has **no CSRF token and no rate limiting** — same as
every other route in this app, but worth calling out specifically here
since this one is destructive and irreversible rather than read-only. Only
run this app somewhere trusted if the clear-down feature is reachable.

### Admin screen (`/admin`) — bulk/system-wide, destructive

Gated by `app.ADMIN_USERNAMES` — a plain Python set of usernames
(matched against `session["username"]` exactly as typed at `/login`, no
re-casing) checked in `_load_client()`'s `before_request` hook: anyone
whose username isn't in that set gets a 403 on `/admin` or any `/admin/*`
sub-route, and the "Admin" nav link (`base.html`, behind
`is_admin_user` — an `app.py` context processor reading the same set)
isn't shown to them either. This is the first real authorization check
in the app — previously `/admin` had none at all, reachable by anyone
logged in who knew the URL. Two independent clear-down actions (plus the
econcur import below), all system-wide rather than scoped to one
patient:

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
do. Still no CSRF token/rate limiting, same as every route in this app —
the `ADMIN_USERNAMES` gate above narrows *who* can reach these, not
what protects the request itself.

**AuditEvent clear-down** — two more actions, both deleting only
`AuditEvent` resources (never the Patient record or any genomic test
data), each with its own confirm step since both can touch
patient-identifiable audit history:

- **One patient** — a small "Patient ID or NHS number" form (no
  checkbox list, unlike the NHS-number-range action above, since this
  targets exactly one patient the admin already knows). `app.
  _resolve_patient_by_id_or_nhs_number()` tries the typed value as a
  Patient id first (`search_patients(patient_id=...)`, which already
  swallows an unknown id into `[]`), then as an NHS number — same
  "take the first match, don't overbuild a disambiguation UI" stance as
  the rest of this screen's small forms. `POST
  /admin/audit-events/patient/confirm` resolves the patient and counts
  their AuditEvents (`client.audit_events_for_patient()`, no date
  bound — safe unbounded here since it's patient-scoped, not
  system-wide) before showing a delete button; `POST
  /admin/audit-events/patient/clear-down` is the only route that calls
  `clear_down_audit_events_for_patient()`. Its result page's "back"
  link (`admin_clear_down_result.html`'s `back_url`/`back_label`
  params) points at that patient's own `/patient/<id>/audit-trail`
  rather than back to `/admin`, since that's the page whose data this
  action just emptied out.
- **All patients** — bounded by a `start`/`end` date range
  (`?start=&end=`, same convention as the audit trail screen itself,
  defaulting to the last 30 days), **not** truly all-time.
  `FhirClient.audit_events_in_range()` requires `start`/`end` (not
  optional, unlike `audit_events_for_patient()`'s) for the same reason
  `_active_orders_with_intent()`'s do — an unbounded system-wide
  AuditEvent query is exactly the shape of query that's 413'd on this
  server before (see "413s on unfiltered system-wide searches"), and an
  audit log is plausibly the largest table on the server, so this is
  the last place to risk an unbounded fetch. `POST
  /admin/audit-events/all/confirm` counts what's in range before
  showing a delete button; `POST /admin/audit-events/all/clear-down` is
  the only route that calls `clear_down_audit_events_in_range()`.

Both share the same `admin_clear_down_result.html` result page as the
other two actions above, and follow the same GET/confirm-lists,
POST-mutates split — the two `*_confirm()` routes never call a
`_delete()`-backed method, only `admin_audit_events_patient_clear_down()`
and `admin_audit_events_all_clear_down()` do.

**econcur import (`/admin/econcur-import`)** — imports NHS ODS's
`econcur.csv` export ("English Hospital Consultants" —
https://digital.nhs.uk/services/organisation-data-service/data-search-and-export/csv-downloads/miscellaneous)
as `Practitioner` + `PractitionerRole` resources, matching existing
entries by GMC number so re-running it updates rather than duplicates
(`FhirClient.import_econcur()`). Runs as a background thread
(`app._run_econcur_import()`, a single in-memory job slot — same
simplicity as `_session_clients`, not a real job queue) since the full
export is tens of thousands of rows, far past what one request should
block on; the page auto-refreshes every 5s while a job is running.
Dry-run vs apply follows `scripts/fix_organization_names.py`'s
`--apply` convention: dry run computes and shows the exact counts apply
would produce without calling `_post()`/`_put()`.

Matching, so a re-run doesn't duplicate — but **`Practitioner` and
`Organization` are create-only**: once matched, neither is ever
rewritten, even if `econcur.csv`'s data for it has since changed (this
replaced an earlier "update if it differs" design for `Practitioner`,
which — combined with the full-table preloads below — was a plausible
contributor to a `413` on a live server, since a re-run against a
~75,000-row export could mean thousands of `PUT`s every single time
regardless of whether anything real had changed):
- `Practitioner`, by GMC-number identifier (`GMC_NUMBER_SYSTEM`) —
  created if the GMC number is unseen, left untouched
  (`practitioners_matched`) if it already exists.
- `Organization` (the row's location organisation code, an ODS trust
  code), by ODS-code identifier (`ODS_ORGANIZATION_CODE_SYSTEM`) — an
  unmatched code gets a minimal stub `Organization` created (ODS code
  only, no name) rather than the row being skipped; an existing one is
  left untouched (`organizations_matched`). `scripts/fix_organization_names.py`
  is still how a stub's name gets backfilled, not this import.
- `PractitionerRole`, by the `(practitioner, organization)` pair, since
  one consultant can hold more than one active membership (separate
  `econcur.csv` rows, same GMC, different org code). A new role is
  created with **three identifiers**: a composite GMC+ODS identifier on
  the role itself (`PRACTITIONER_ROLE_GMC_ODS_SYSTEM` =
  `https://fhir.nwgenomics.nhs.uk/Identifier/PractitionerRole-GMC-ODS`,
  value `"<gmc>-<ods>"`), plus an `identifier` (not just a bare
  `.reference`) on each of the role's own `practitioner`/`organization`
  references, carrying that target's GMC number / ODS code respectively
  — so a `PractitionerRole` resource is self-describing about who/where
  it's for without needing to dereference either link. **A role that
  already carries all three identifiers is "settled" and is never
  updated again** (`roles_unchanged`,
  `FhirClient._econcur_role_has_identifiers()`) — same create-only
  reasoning as `Practitioner`/`Organization` above, and for the same
  reason (this was the other half of the write-volume-per-rerun problem:
  a specialty-diff check on every one of tens of thousands of roles,
  every run). A role that predates this identifier scheme (missing one
  or more) gets them backfilled **once** — refreshing `specialty` to
  match `econcur.csv` at the same time, since it's already being written
  (`roles_updated`) — which is what makes it settled from then on.

The Practitioner/PractitionerRole matching dicts are preloaded once per
run, **organisation by organisation** (`_search_all_by_organization()`),
rather than searched per row or via one unfiltered system-wide dump —
the latter is what actually 413'd on a live server; see "413s on
unfiltered system-wide searches" above for the full story, including the
per-GMC fallback search (`_find_practitioner_by_gmc()`) that covers the
one gap organisation-batching alone can't. `Organization` itself
(`all_organizations_by_ods()`) is still one unfiltered search — the org
list this batching is built *from* obviously can't itself be batched by
organisation. `MAIN_SPECIALTY_CODE_SYSTEM` (UK Core's
`https://fhir.hl7.org.uk/CodeSystem/UKCore-PracticeSettingCode`) is used
for `PractitionerRole.specialty.coding.system`; the bare specialty code
(e.g. `"300"`) is stored regardless of whether that URI is the one this
server expects.

### Cepheid Test Results (`/cepheid-results`)

Cross-patient, system-wide, bounded to `DiagnosticReport.date` within a
`start`/`end` range (`?start=&end=`, defaulting to the last 30 days —
same convention as `/stats`): `DiagnosticReport`s with a BCRABL code.
`FhirClient.bcrabl_reports(start, end)`'s FHIR query itself is filtered
by `code` — `BCRABL_CODES` (`BCRABL_CODE` = `"BCRABL"`, plus
`BCRABL_LOINC_CODE` = LOINC `"69380-4"`), joined bare (no `system|`
prefix, via `_bcrabl_code_search_value()`) since the coding system is
unconfirmed. This used to be a `category=Genetics` search with no code
restriction at all — filtering only client-side after fetching
everything — which 413'd on a live server the same way `ctdna_orders()`'s
DiagnosticReport-side query did; `code` is now required in both the
categorized and fallback query attempts, not optional. `_is_bcrabl_report()`
still re-checks `coding[].code in BCRABL_CODES` client-side after
fetching (a bare-code token search can match a coincidental hit
elsewhere on the resource, so this isn't redundant), but the FHIR query
is what keeps the result set small now, not just the post-fetch filter.
Distinct from both the Genomic Test Directory code (confirmed system,
matched via `test_directory_code()`) and ctDNA (matched via
`CTDNA_TEST_DIRECTORY_CODES` where confirmed, `CTDNA_TEXT_MATCHES` as
fallback). Query shape otherwise mirrors `ctdna_orders()`/
`active_filler_orders()`: `_include`s specimen/patient/result plus the
originating order via `_include=DiagnosticReport:based-on` — a
**forward** include this time (the report references the order via its
own `basedOn`, so no revinclude is needed, unlike `ctdna_orders()` which
searches from the ServiceRequest side and needs `_revinclude` to reach
the report) — and identifies reports by `resourceType` across
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

### Audit trail (`/patient/<id>/audit-trail`)

A patient-scoped view of `AuditEvent` resources — who accessed or changed
this patient's record, when, and how — reached from a "🕵 View Audit Trail"
link at the top of the patient page.

- **`FhirClient.audit_events_for_patient(patient_id, start, end)`** tries
  R4's composite `patient` search param first (defined to resolve either
  `agent.who` or `entity.what` to a Patient — the parameter actually meant
  for "everything about this patient's audit trail"), falling back to the
  narrower `entity=Patient/<id>` for a server that indexes
  `AuditEvent.entity` but not the composite param — same
  try-then-fall-back stance the category-code searches
  (`lab_orders_for_patient()` etc.) use elsewhere in this file. Bounded by
  `date` (`AuditEvent.recorded`) via the same `[ge, le]` repeated-param
  convention `orders_in_range()`/`reports_in_range()` use — optional here,
  unlike those, since this is already patient-scoped rather than
  system-wide, so it isn't the 413 risk those were rewritten to avoid.
  `app.patient_audit_trail()` defaults `start`/`end` to the last 30 days
  (`?start=&end=`, same convention as `/stats`/`/ctdna`/
  `/cepheid-results`).
- **`action`/`outcome` are rendered from FHIR R4's own fixed ValueSets**
  (`FhirClient.AUDIT_EVENT_ACTIONS` = C/R/U/D/E →
  Create/Read/Update/Delete/Execute, `AUDIT_EVENT_OUTCOMES` = 0/4/8/12 →
  Success/Minor/Serious/Major failure) — hardcoded, not guessed, since
  these are spec-fixed rather than deployment-specific (same reasoning as
  `ASK_AT_ORDER_ENTRY_QUESTIONS`).
- **`type`/`subtype` are a bare `Coding`, not a `CodeableConcept`** — per
  the AuditEvent resource shape, unlike `ServiceRequest.code` and most
  other coded fields in this app — so the template reuses the
  `coding_text` filter (the same one `Encounter.class` uses) rather than
  `code_text`.
- **Agent display** (`FhirClient.audit_event_agent_display()`) prefers
  `agent.name` (the field the spec defines specifically for a
  human-meaningful label), then a resolved `agent.who` reference's
  `.name[]`, then that reference's own inline `.display`/`.identifier` —
  same resolve-then-fall-back-to-logical-reference stance
  `_resolve_practitioner_display()` uses for a requester's practitioner
  reference. **Source display** (`audit_event_source_display()`) is
  `source.observer`'s inline `.display` only, unresolved — typically a
  Device with nothing more useful to fetch.
- **No Entity(ies) column** — dropped in favour of three narrower,
  specifically-useful columns pulled from individual `AuditEvent.entity`
  entries instead of listing every entity generically:
  - **Message ID** / **Correlation ID** — the entity whose `.type.code`
    is `XrequestId` / `XcorrelationId` respectively
    (`FhirClient.AUDIT_ENTITY_MESSAGE_ID_CODE`/
    `AUDIT_ENTITY_CORRELATION_ID_CODE`, matched via
    `_audit_entity_by_type_code()`) — local codes, **system
    unconfirmed**, so matched by code alone regardless of system, same
    "code confirmed, system not" stance as `BCRABL_CODE`. The actual ID
    value's location on that entity is *also* unconfirmed, so
    `audit_event_message_id()`/`audit_event_correlation_id()` go through
    `audit_event_entity_display()`, which now also falls back to
    `what.identifier.value` (added for this — a request/correlation ID
    is plausibly modelled as a logical identifier, not just free text)
    before `.display`/`.reference`. **The Message ID column is further
    shortened for display** (`app.audit_message_id_short()`, composed
    into the `audit_message_id` Jinja filter) — this deployment's raw
    value is shaped `"<system>.<instance>.<queue>:<id>"` (e.g.
    `"RIE.Production.ESBDevelopment:885859"`), and only the part before
    the first `"."` plus the part after the last `":"` is shown
    (`"RIE 885859"`) — dropping the middle segments and queue name,
    which aren't useful here. Falls back to the value unchanged if it
    doesn't contain both a `"."` and a `":"` (including the `"—"`
    placeholder for "no Message ID entity found"), rather than guessing
    at a differently-shaped value. Correlation ID is **not** shortened
    the same way — it's also the filter column below, and needs to stay
    the real value to match against.
  - **Query** — the entity whose `.type` is
    `http://terminology.hl7.org/CodeSystem/audit-entity-type|2`
    ("System Object" — `AUDIT_ENTITY_TYPE_SYSTEM`/
    `AUDIT_ENTITY_QUERY_TYPE_CODE`, both spec-fixed so matched on system
    *and* code, unlike the two local codes above). Its `.query` is
    base64Binary per the AuditEvent spec; `audit_event_query_text()`
    base64-decodes it to plain text, falling back to the raw
    (undecoded) value if it isn't valid base64 rather than hiding a
    malformed-but-present value, and returning `None` (not "—") when
    there's no such entity at all — the template only renders a `<code>`
    block when there's real decoded text to show.
- **Recorded is split into separate Date/Time columns**
  (`app.audit_recorded_date()`/`audit_recorded_time()`) — a plain
  `split("T", 1)` on the `AuditEvent.recorded` instant string, not a
  datetime parse/reformat, since the FHIR wire format already guarantees
  that separator. Time is further trimmed to plain `HH:MM:SS`
  (`[:8]` on the post-"T" part) — the instant grammar always puts a
  fixed-width `HH:MM:SS` first, optionally followed by fractional
  seconds and/or a timezone offset (`.123`, `Z`, `+01:00`, ...), which
  are dropped rather than shown.
- **Filterable by Correlation ID (`?correlation_id=`) and Message ID
  (`?message_id=`)** — each a `<select>`, not free text, offering only
  the distinct values actually present in the current date-bounded
  result set (`app.patient_audit_trail()` builds `correlation_ids`/
  `message_ids` as `sorted({...})` over every fetched event, **before**
  either filter is applied — so each option list always reflects the
  full range, not just whatever's currently selected, and independently
  of what the *other* filter is set to). Filtering itself is an exact
  match, applied client-side after the date-bounded fetch rather than as
  a separate FHIR search — neither is a real search parameter, each is
  dug out of one specific entity's fields the same way its table column
  is (see above). Both filters combine with AND when both are set.
  Message ID's `<select>`/filter match on the same shortened form the
  column displays (`audit_message_id_short()`, see above) rather than
  the raw entity value, so the dropdown reads the same as the table.
  **Each `<select>` lives inside its own column's `<th>`** (not a
  separate control up by the date range) — the `<table>` sits inside the
  same `<form>` as the date pickers, so submitting via the top "Update"
  button carries both filter params along with `start`/`end`.
  **`onchange="this.form.submit()"` on both `<select>`s** — picking a
  value submits the form immediately, since a bare `<select>` inside a
  `<form>` doesn't submit on its own (an earlier version required
  clicking the far-away top "Update" button afterwards, which looked
  like the dropdown just didn't filter at all). **The table (and so both
  `<select>`s) stays rendered even when the current filter(s) match zero
  events** — the row loop's Jinja `{% else %}` prints a "No events match
  the selected Message ID / Correlation ID." row instead of the whole
  `{% if events %}` block being skipped, since skipping it would make
  both `<select>`s disappear along with the table the moment a filter
  produced no rows, trapping the user with no visible way to reset back
  to "All". The plain "No audit events found..." message is only shown
  when there's truly nothing at all for the range (no events *and* no
  correlation IDs *and* no message IDs to offer).

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
6. **ctDNA matching** (`_is_ctdna_order` / `CTDNA_TEST_DIRECTORY_CODES` /
   `CTDNA_TEXT_MATCHES`) — `M4.14` is confirmed as a Genomic Test Directory
   code for ctDNA, checked first; `code` text is still the fallback for
   anything that doesn't carry it. If `/ctdna` comes back empty against a
   real server, check what `code.coding[]` (system
   `GENOMIC_TEST_DIRECTORY_SYSTEM`) and `code.text`/`code.coding[].display`
   actually say on a known ctDNA order — add any other codes you find to
   `CTDNA_TEST_DIRECTORY_CODES`, or adjust the text match list.
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
12. **GMC/GMP identifier system URIs** (`GMC_NUMBER_SYSTEM` =
    `https://fhir.hl7.org.uk/Id/gmc-number`, `GMP_NUMBER_SYSTEM` =
    `https://fhir.hl7.org.uk/Id/gmp-number`, used by
    `FhirClient._practitioner_registration_code()` to append a
    requesting clinician's registration number in brackets after their
    name — see Requester resolution below) — standard UK Core system
    URIs, not confirmed against a real server's Practitioner resources.
    If a requesting clinician's registration number never appears in
    brackets on `/patient/<id>`, `/work-orders`, `/test-orders`,
    `/ctdna`, or the `/stats` organisation/ICS drill-downs, sample a
    real Practitioner resource's `identifier` array and adjust the
    system URI(s).
13. **AuditEvent search params on this server**
    (`FhirClient.audit_events_for_patient()`, used by
    `/patient/<id>/audit-trail`) — whether this server indexes the
    composite `patient` search param at all, and whether it populates
    `AuditEvent` for patient record access in the first place, are both
    unconfirmed. If the audit trail screen always comes back empty for a
    patient known to have activity, check whether the `entity=
    Patient/<id>` fallback fares any better, and separately confirm the
    server is generating/storing `AuditEvent` resources at all.
14. **Message ID / Correlation ID entity codes and value location**
    (`AUDIT_ENTITY_MESSAGE_ID_CODE` = `"XrequestId"`,
    `AUDIT_ENTITY_CORRELATION_ID_CODE` = `"XcorrelationId"`, matched on
    `entity.type.code` alone — system unconfirmed) — if the Message
    ID/Correlation ID columns on `/patient/<id>/audit-trail` are always
    "—", sample a real `AuditEvent.entity` array and check both whether
    these codes are right and which field (`.name`, `.description`,
    `.what.identifier.value`, `.what.display`) actually carries the ID
    value — `audit_event_entity_display()` checks all of them in that
    order but was written without a real example to check against.

### Epic FHIR connectivity (`epic_client.py`)

A second, separate FHIR integration alongside `fhir_client.py`'s
`FhirClient`, surfaced in the app as the **Pathology Explorer** nav item
(renamed from "Epic" — see "Pathology Explorer" further down this
section) — nothing here cross-references a NW GMSA `Patient`/order/report
id, and `app.py`'s `/pathology*` routes are the only ones that call into
this module; every other route still only touches `g.client`/the NW
GMSA `FhirClient` session. The nav's own home link was renamed too, from
"Lab Explorer" to **"NW Genomic Explorer"** (`templates/base.html`,
both the `<title>` and the nav's `&larr;` link back to `/`) — now that
there's a second, unrelated FHIR-backed screen in the same nav, the
original generic "Lab Explorer" name no longer clearly meant *this
app's own NW Genomics server* specifically. Where
`FhirClient` authenticates per-user via HTTP Basic against the NW GMSA
server this whole app is otherwise built around, `EpicClient`
authenticates as a registered **backend application** (no user session,
no username/password) against an Epic FHIR R4 endpoint, via SMART
Backend Services (OAuth2 JWT-bearer client-credentials — a JWT signed
with a private key registered on fhir.epic.com is traded for a bearer
token; no client secret is ever sent). The plan is to start linking the
two once there's a real **Manchester Foundation Trust (MFT)
non-production/test Epic instance** to develop against — the initial
target in the meantime is **Epic's own public non-production sandbox**
(`EPIC_FHIR_BASE_URL_DEFAULT` =
`https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4`, per
https://fhir.epic.com/Documentation?docId=testingguide — confirmed
directly, this app's one hardcoded Epic default since it's a stable
public URL, not a secret or deployment-specific value). Every other
value comes from environment variables with no default
(`EPIC_CLIENT_ID`/`EPIC_PRIVATE_KEY_PATH` or `EPIC_PRIVATE_KEY`/
`EPIC_SCOPE`; `EPIC_TOKEN_URL`/`EPIC_JWT_KID`/`EPIC_VERIFY_SSL` are
optional — see `epic_client.py`'s `EpicClient.config()` docstring for
all of them). Needs `pyjwt[crypto]` (RS384 JWT signing; Python's
standard library can't do RSA signing on its own) — added to
`requirements.txt` — but imported optionally (`jwt = None` on
`ImportError`), so the app still starts without it or any `EPIC_*`
variable set: only an actual Epic call fails, with a `RuntimeError` the
`/epic`/`/pathology*` routes already show as an error. `config()` also
turns an unreadable `EPIC_PRIVATE_KEY_PATH` into a `RuntimeError` (not a
bare `FileNotFoundError`) for the same reason. **`.env` is only actually read if something calls
`load_dotenv()`** — `app.py` does this once, right after its imports
(before `app = Flask(...)`), since every env var in this app (Epic's
included) is read lazily inside a function/`__init__` rather than at
import time, so it only needs to run before the first request, not
before the other imports. This was missing for a while (the whole point
of `python-dotenv` being in `requirements.txt`), which meant a fully
filled-in `.env` had no effect at all unless the values were also
`export`ed in the shell — the actual root cause the one time an "Epic
login isn't working" report was debugged end-to-end, alongside two
separate contributing issues that also had to be fixed before
`EpicClient.verify_connection()` succeeded: `pyjwt` not actually
installed in the venv despite being in `requirements.txt` (re-run `pip
install -r requirements.txt`), and the local `epic_private_key.pem`
(git-ignored, per `.gitignore`'s `*.pem`/`/epic_private_key*`) missing
from a fresh checkout — regenerate it with
`scripts/generate_epic_jwks.py --kid <matching EPIC_JWT_KID> --replace`
*only* if the original truly can't be recovered, since replacing it
invalidates whatever Epic already has registered until the new public
half in `epic/jwks.json` is committed, pushed, and re-fetched by Epic.

**The JWKS Epic verifies these JWTs against is hosted from this GitHub
project, not served by the Flask app** —
`scripts/generate_epic_jwks.py` generates an RSA key pair, writes the
private key to a local git-ignored PEM file (`*.pem`/
`/epic_private_key*` in `.gitignore` — never commit it), and
adds/updates its public half as a JWK in `epic/jwks.json` (which *is*
committed). That file's GitHub raw URL
(`https://raw.githubusercontent.com/nw-gmsa/julius/main/epic/jwks.json`)
is what gets registered as this app's JWKS URL on fhir.epic.com, keyed
by `--kid`/`EPIC_JWT_KID` — supports key rotation (re-run with a new
`--kid` to add a second key rather than replacing the first; `--replace`
to actually replace one). Verified directly: a JWT signed with a
generated private key decodes correctly against the matching public key
pulled from the generated `epic/jwks.json`, using the same `RSAAlgorithm.
from_jwk()`/`RS384` verification path Epic's own authorization server
would use. **Not yet confirmed: whether this repo (or at least this one
file, e.g. via GitHub Pages) is actually publicly fetchable** — Epic's
servers need to reach that URL over the open internet to verify a token
request; check this before registering it.

`EpicClient.verify_connection()` is the one thing worth running today
against the sandbox: fetches the server's `CapabilityStatement`
(`/metadata`, unauthenticated per spec, so this alone confirms
`EPIC_FHIR_BASE_URL` is reachable) and then acquires an access token
(confirming the client id/key/scope are accepted) — still needs a real
`EPIC_CLIENT_ID` from registering this app on fhir.epic.com's sandbox
first. `EpicClient.get(path, params=None)` is a generic authenticated
FHIR GET behind everything else here.

**Pathology Explorer** (`/pathology`, `/pathology/search`,
`/pathology/patient/<id>`, `app.py`) — the nav item renamed from "Epic"
above, and the first thing in this app that actually calls into
`epic_client.py` from a route. Deliberately
broader than the NW Genomics side: Epic's `DiagnosticReport` set spans
ordinary pathology/lab results as well as genomics, and this screen
shows all of it for a patient (`diagnostic_reports_for_patient(patient_id,
category=None)` — every category, not just genetics), not just the
genomics-only slice the NW Genomics `/patient/<id>` page shows for its
own server. Mirrors the NW Genomics side's search-then-browse shape
(`/pathology` ~ `/`, `/pathology/search` ~ `/search`,
`/pathology/patient/<id>` ~ `/patient/<id>`) but is much thinner — no
orders, no specimens, no order chains, since `epic_client.py` doesn't
read `ServiceRequest`/`Specimen` at all yet, only `Patient`/
`DiagnosticReport`/`Observation` (plus `FamilyMemberHistory`, not wired
into a route yet — see "Family history / pedigree" below).

- **`EpicClient.search_patients(family=None, given=None, birthdate=None,
  identifier=None, patient_id=None)`** / **`get_patient(patient_id)`** —
  the Pathology Explorer's counterpart to `FhirClient.search_patients()`/
  `get_patient()`. `identifier` takes priority over name/DOB fields if
  both are given, rather than combining them into one query. Refuses to
  run a fully unfiltered query (`[]` if nothing at all is given) — same
  not-worth-the-risk stance `FhirClient.search_patients()`/
  `search_organizations()` take against their own server (see "413s on
  unfiltered system-wide searches" above), though as it turns out **Epic
  enforces this itself anyway**: a bare `Patient?_count=20` with no
  `_id`/demographic param, tried directly against the sandbox, comes
  back `400` with `"This resource requires demographics or _id parameter
  for searching."` (`DiagnosticReport` has the same guard: `"...requires
  a patient or _id parameter for searching."`) — there's no wildcard/
  browse-everything path on Epic's side to fall back to even if this
  app wanted one.
- **Name search (`family`/`given`) is confirmed *not* to find a patient
  known to exist, on this backend client** — `family=Lopez,
  given=Camila` against Epic's sandbox reliably comes back a genuine,
  error-free zero-match for a patient (Camila Lopez) that direct id/MRN
  lookup finds immediately. **`identifier` (MRN, with or without the
  `urn:oid:1.2.840.114350.1.13.0.1.7.5.737384.14|` system prefix) and
  direct `patient_id` (a FHIR id) both work, verified directly** against
  several of the patients in `docs/epic-sandbox-test-patients.md`. Not
  yet root-caused *why* name search comes back empty specifically for a
  backend/system-level client (as opposed to Epic's interactive
  patient-facing sandbox, which is what the well-known named test
  patients like "Camila Lopez" are primarily documented for) — until
  that's understood, `pathology_index.html`'s search form should keep
  leading with identifier/patient-id fields rather than name, and anyone
  extending this should reach for `docs/epic-sandbox-test-patients.md`'s
  known-good MRNs/FHIR ids rather than guessing a name.
- **`EpicClient._entries(bundle, resource_type)`** — every
  `Bundle`-returning method (`search_patients()`,
  `diagnostic_reports_for_patient()`, `family_history_for_patient()`)
  goes through this rather than a bare `[e["resource"] for e in entries
  if "resource" in e]`. Confirmed directly against Epic's sandbox: a
  zero-match search doesn't come back with an empty `entry` list, it
  bundles one entry wrapping an `OperationOutcome` ("Resource request
  returns no results.", warning severity) — a naive filter treated that
  as if it were a matched `Patient`/`DiagnosticReport`/etc, which was a
  real bug hit directly while building the Pathology Explorer
  (`search_patients(family="Smith")` returning one result with a blank
  name and a dead `/pathology/patient/` link for a search that had
  actually matched nobody). This also means
  `diagnostic_reports_for_patient()`'s `not entries` fallback-to-
  unfiltered-search check could never previously have triggered on a
  real zero-match response, since `entries` was never actually empty —
  fixed as part of the same change.
- **`docs/epic-sandbox-test-patients.md`** lists the confirmed-real test
  patients (name, FHIR id, external id, MRN, and which FHIR resource
  types each one actually has data for) discovered while getting this
  screen working end-to-end — Epic's own public testing-guide docs
  name several of these patients but not their ids/MRNs, and the
  dedicated sandbox-test-patients doc page is JS-rendered (not
  scrapable). Of the seven listed, only **Camila Lopez** and **Warren
  McGinnis** have `DiagnosticReport` data — verified end-to-end via
  `/pathology` (Lopez: 7 reports including 2 Pharmacogenomic Panels;
  McGinnis: Stress test + Cholesterol total) — the other five are
  scoped to different resource types (conditions, medications,
  immunizations, documents, ...) and will resolve as a patient but show
  nothing under "Diagnostic reports".
- **`epic_client.py`'s `KNOWN_SANDBOX_TEST_PATIENTS`** is that same
  patient list as data (name/`fhir_id`/`external_id`/`mrn`/
  `has_diagnostic_reports`), and `pathology_index.html` renders it as a
  "Known sandbox test patients" quick-select table — a direct
  `/pathology/patient/<fhir_id>` link per row — on both `/pathology`
  and its `/pathology/search` results page. This exists specifically
  *because* name search doesn't reliably work here (see above): without
  a known-good id to jump straight to, there'd be no reliable way to
  reach a real patient on this sandbox through the UI at all.
- **The patient page's demographics render as a banner** (`.patient-banner`
  in `templates/base.html`, a shaded card above "Diagnostic reports"
  rather than a plain `<table>`) — name as the heading, then DOB/gender/
  every identifier (via the new `all_identifiers` Jinja filter,
  `app.py` — the same generic "show every identifier" formatter the
  Cepheid Test Results screen already used internally, now also
  registered as a filter) inline underneath, plus the FHIR id itself.
  "Diagnostic reports" (renamed from "Pathology & genomics reports" —
  same data, `reports`/`report_observations`) follows directly below the
  banner, each report card still showing its resolved Observation
  results table as before.
- **Orders and Documents** — two more sections below "Diagnostic
  reports", backed by `EpicClient.service_requests_for_patient()`
  (`ServiceRequest?patient=<id>`) and `document_references_for_patient()`
  (`DocumentReference?patient=<id>`), both plain unfiltered-by-category
  searches (there's no confirmed genetics-only category to narrow
  `ServiceRequest` by the way `diagnostic_reports_for_patient()` can for
  reports). **Both search scopes are granted for this app's client even
  though `EPIC_SCOPE` in `.env` doesn't list them** — confirmed directly
  from a real token response, which came back with a broader scope
  (`Binary`, `Condition`, `ServiceRequest`, `DocumentReference`, ...)
  than what was actually requested; Epic's sandbox appears to grant
  whatever the app's own registration allows, regardless of the
  `scope` value sent on the token request. Verified end-to-end for
  Camila Lopez: 21 ServiceRequests, 6 DocumentReferences. A
  `DocumentReference` can carry its document inline as base64 in
  `content[].attachment.data` — potentially large — which the "View
  FHIR" dialog (below) shows as-is, nothing strips or truncates it.
- **"View FHIR" — a shared dialog for viewing any resource's raw,
  pretty-printed JSON, with a Copy button** (`templates/base.html`: a
  native `<dialog id="fhir-dialog">`, no framework/polyfill, same
  minimal-vanilla-JS convention as `order_new.html`'s AOE show/hide
  logic). Any page can wire a resource into it: embed the resource as
  `{{ resource|tojson }}` inside a `<script type="application/json"
  id="...">` block (Flask's built-in `tojson` filter — not a custom
  one — since it's specifically pre-escaped safe for embedding inside a
  `<script>` block, unlike dumping raw JSON into an HTML attribute like
  `onclick`, which its `</`/quote escaping isn't meant for), then call
  `showFhirResource('that-id', 'Title shown in the dialog')` from a
  button's `onclick`. `showFhirResource()` re-parses that JSON and
  re-serializes it with `JSON.stringify(obj, null, 2)` for the pretty
  (indented) display — deliberately re-stringified client-side rather
  than trying to pretty-print server-side, so the exact same helper
  works for a resource of any shape/size with zero server changes.
  `fhirDialogCopy()` copies the dialog's current pretty-printed text via
  `navigator.clipboard.writeText()`. `pathology_patient.html` wires this
  up for the Patient (banner), every DiagnosticReport (and each of its
  Observation result rows), ServiceRequest, and DocumentReference row —
  reuse the same `showFhirResource()`/embed pattern rather than a
  bespoke viewer if another screen needs "show me the raw resource"
  (the dialog markup/JS lives in `base.html` specifically so it's
  available everywhere, not just `/pathology*`). Both the `<dialog>`
  itself and its scrollable `<pre>` set `overscroll-behavior: contain`
  — without it, scrolling a resource too tall for the dialog's
  `max-height` would hit the `<pre>`'s scroll boundary and chain the
  rest of the scroll gesture through to the page behind (a real bug hit
  directly on a large resource, e.g. one of the ServiceRequests with a
  long `note`/`supportingInfo`), which reads as "the popup scrolled the
  background" — `contain` stops a scroll from propagating past the
  element that's actually being scrolled once it runs out of room.
- **"View document"** (`/pathology/document/<document_id>?index=N`,
  `app.py`) — streams one of a DocumentReference's actual attachments
  (not its FHIR JSON — that's "View FHIR" above), the Pathology
  Explorer's counterpart to `/report/<report_id>/pdf`. Doesn't need a
  `patient_id` on the URL: `EpicClient.get_document_reference(document_id)`
  is a direct `Read`, unlike the `Search`-based methods elsewhere in
  this module that need a patient to scope by (Epic's search-requires-
  a-parameter business rule — see above — doesn't apply to a plain
  id-based `Read`). One link per `content[]` entry (usually just one),
  labelled with that attachment's content type.
  `EpicClient.fetch_attachment_bytes(attachment)` mirrors
  `FhirClient.fetch_attachment_bytes()` exactly: inlined base64 `.data`,
  or a `.url` pointing at a **Binary** resource, requested as `Accept:
  application/fhir+json` and decoded (falling back to raw bytes if a
  server ignores the Accept header). **Confirmed directly: one of
  Camila Lopez's own documents (an `application/pdf` attachment) fails
  server-side on Epic's own sandbox** — `400`, `"Unknown error occurred
  formatting binary content."` — regardless of `Accept` header tried
  (`application/fhir+json` or the attachment's own `application/pdf`);
  a sibling `text/html` attachment on the same patient resolves fine
  either way, so this isn't a request-format bug on this app's side.
  Not swallowed — surfaces as a `502` with Epic's own error text, same
  "surface it" stance as `/report/<report_id>/pdf`'s failure handling.

**Genomic reports** — `diagnostic_reports_for_patient(patient_id,
category=DIAGNOSTIC_REPORT_GENETICS_CATEGORY)` searches `DiagnosticReport`
by the same HL7 v2-0074 `"GE"` category `fhir_client.py` uses for NW
GMSA, falling back to an unfiltered patient search if the categorized
one comes back empty (same try-then-fall-back pattern as
`fhir_client.py`'s category searches) — a real HL7-standard code, still
*not* confirmed as the exact category Epic's sandbox tags its genomics
reports with (Camila Lopez's 2 "Pharmacogenomic Panel" reports, found via
the Pathology Explorer's `category=None` search, haven't been checked
for whether `category=DIAGNOSTIC_REPORT_GENETICS_CATEGORY` alone would
have found them too). `observations_for_report(report)`
resolves the report's `result[]` Observation references, skipping (not
raising on) any one that fails to resolve. Epic's specification portal
separately confirms a dedicated `Observation.Search`/`.Read (Genomics)
(R4)` operation exists, but the exact category/profile it expects
couldn't be confirmed from the (JS-rendered, not scrapable) public docs
site — not implemented as its own method yet; extend
`diagnostic_reports_for_patient`'s pattern once that's confirmed against
a real sandbox session.

**Family history / pedigree** — `family_history_for_patient(patient_id)`
is a plain `FamilyMemberHistory?patient=<id>` search (Epic confirms
Search/Read support for this resource in R4). Standard
`FamilyMemberHistory` has **no extension linking one entry to another as
a pedigree "parent"** — each entry just describes one relative's history
relative to the *patient*, via a `relationship` code from HL7's
v3-RoleCode `FamilyMember` value set (confirmed directly against
`terminology.hl7.org`'s own 107-code expansion, e.g. `"MGRFTH"` =
maternal grandfather, `"PAUNT"` = paternal aunt) — and those codes
already encode the relative's tree position, so a browsable family view
doesn't need a separate pedigree resource. `relationship_info(fmh)`
returns `(label, generation, side)` for one resource (generation:
positive = ancestor generations, e.g. 1 = parent, 2 = grandparent;
negative = descendant generations, e.g. -1 = child/niece/nephew; 0 =
same generation as the patient — siblings, cousins, spouse, in-laws;
side: `"maternal"`/`"paternal"` where the code itself encodes which side,
else `None`) via `FAMILY_RELATIONSHIP_INFO`, a code -> (generation, side)
table covering the full confirmed value set.
`group_family_history(family_member_histories)` buckets a patient's list
by `{generation_or_"other": {side_or_"unspecified": [...]}}` — an
unrecognised relationship code lands in `"other"` rather than being
dropped. **Now rendered** by the Pathology Explorer's "Family history"
section (`/pathology/patient/<id>`) — `app.py`'s
`family_history_sections()` reshapes `group_family_history()`'s dict
into a flat, ordered list of `(generation_label, [(side, entries), ...])`
tuples (`family_generation_label()`/`family_generation_sort_key()`/
`family_side_sort_key()`, all in `app.py`) so the template can just loop
over it in reading order — oldest ancestor generation first, down
through descendants, "Other relatives" last; maternal before paternal
before unspecified within each generation — rather than the arbitrary
dict/insertion order `group_family_history()` itself produces. Verified
directly with synthetic multi-generation data (mother/father/sister/
maternal grandmother/daughter/an "other"-bucket extended relative) since
**none of the 7 patients in `docs/epic-sandbox-test-patients.md` have
any real FamilyMemberHistory data** — the search itself succeeds
cleanly (no error, no `_entries()` OperationOutcome-shaped surprise),
it's a genuine zero for all seven. Each row shows the relative's name,
relationship label, `condition[]` (joined via `code_text`), a
deceased/status column (`deceasedBoolean`/`deceasedAge`/
`deceasedDateTime`/`status`, checked in that order), and a "View FHIR"
button.

**Conditions** — `EpicClient.conditions_for_patient(patient_id)` is a
plain `Condition?patient=<id>` search (`system/Condition.read` is
granted for this client, same already-granted-without-being-in-
EPIC_SCOPE situation as `service_requests_for_patient()`/
`document_references_for_patient()` above), rendered as its own
"Conditions" section on `/pathology/patient/<id>` between "Diagnostic
reports" and "Orders" — Epic's problem list, distinct from a
FamilyMemberHistory entry's own `condition[]` (that's about a
*relative's* condition, this is the patient's own). **Verified directly
against Camila Lopez: 2 real Condition resources, both category
"Genomic Indicators"** (`https://open.epic.com/FHIR/StructureDefinition/
condition-category` code `"genomics"`) — pharmacogenomic metabolizer
statuses ("CYP2B6 Intermediate Metabolizer", "CYP2C9*6: AA (wildtype)")
whose `.evidence[]` points back at the same Observation her
Pharmacogenomic Panel DiagnosticReports already surface elsewhere on
the page, not a separate/unrelated dataset — a real example of exactly
the "pathology plus genomics" scope this whole screen was built for.
Each row shows the condition, category, clinical status, and a date
(`onsetDateTime`/`recordedDate`/`onsetString`, checked in that order,
since which of these a given Condition populates varies), plus "View
FHIR".

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
