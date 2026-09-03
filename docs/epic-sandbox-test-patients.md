# Epic sandbox test patients

Named test patients on Epic's public non-production FHIR sandbox
(`EPIC_FHIR_BASE_URL_DEFAULT` in `epic_client.py`), for use with the
Pathology Explorer (`/pathology`) and anything else built against
`epic_client.py`'s `EpicClient`.

## What actually works for a backend-systems client

This app authenticates as a **backend system** (SMART Backend Services,
no user/patient context — see `epic_client.py`'s module docstring), not
as an interactive/patient-facing app. Verified directly against the
patients below:

- **Direct id lookup** (`EpicClient.get_patient(fhir_id)` /
  `/pathology/search?patient_id=<fhir id>`) — works.
- **MRN/identifier search** (`EpicClient.search_patients(identifier=...)`
  / `/pathology/search?identifier=<mrn>`) — works, with or without the
  `urn:oid:1.2.840.114350.1.13.0.1.7.5.737384.14|` system prefix (a bare
  MRN value matches the same way `FhirClient.search_patients()`'s
  `nhs_number` search does on the NW GMSA side).
- **Name search** (`family`/`given` — `EpicClient.search_patients(family=...,
  given=...)`) — confirmed **not** to work for this backend client, even
  for a patient known to exist (e.g. `family=Lopez, given=Camila`
  reliably returns a genuine zero-match, not an error). Don't rely on
  name search here; use the MRN or FHIR id instead.
- **Unfiltered/wildcard search** on `Patient` or `DiagnosticReport` (no
  `_id`/`identifier`/`patient` param at all) — rejected outright by
  Epic's server with a `400` business-rule error ("This resource
  requires demographics or `_id` parameter for searching" /
  "...requires a patient or `_id` parameter for searching"). Not a bug
  on this app's side — Epic enforces this itself.

The MyChart username/password below are for logging into Epic's
**interactive patient-facing** sandbox (MyChart / SMART patient launch)
directly on Epic's own site — not usable by this app's backend-services
client, which never authenticates as a patient. Kept here for
reference/completeness only.

## Patients

### Camila Lopez
- **FHIR id**: `erXuFYUfucBZaryVksYEcMg3`
- **External id**: `Z6129`
- **MRN**: `203713`
- **MyChart**: `fhircamila` / `epicepic1`
- **Resources**: DiagnosticReport, Goal, Medication, MedicationOrder,
  MedicationRequest, MedicationStatement, Observation (Labs), Patient,
  Procedure
- Confirmed working end-to-end via `/pathology` — 7 DiagnosticReports
  (CBC and differential, Hemoglobin A1c, 2× Pharmacogenomic Panel, X-ray
  Chest 2 Views, Transthoracic Echo, Specimen to pathology), most with
  resolvable Observation results.

### Derrick Lin
- **FHIR id**: `eq081-VQEgP8drUUqCWzHfw3`
- **External id**: `Z6127`
- **MRN**: `203711`
- **MyChart**: `fhirderrick` / `epicepic1`
- **Resources**: CarePlan, Condition, Goal, Medication, MedicationOrder,
  MedicationRequest, MedicationStatement, Observation (Smoking History),
  Patient

### Desiree Powell
- **FHIR id**: `eAB3mDIBBcyUKviyzrxsnAw3`
- **External id**: `Z6130`
- **MRN**: `203714`
- **MyChart**: `fhirdesiree` / `epicepic1`
- **Resources**: Immunization, Observation (Vitals), Patient

### Elijah Davis
- **FHIR id**: `egqBHVfQlt4Bw3XGXoxVxHg3`
- **External id**: `Z6125`
- **MRN**: `203709`
- **Resources**: AllergyIntolerance, Binary, Condition, DocumentReference,
  Medication, MedicationOrder, MedicationRequest, MedicationStatement,
  Observation (Smoking History), Patient

### Linda Ross
- **FHIR id**: `eIXesllypH3M9tAA5WdJftQ3`
- **External id**: `Z6128`
- **MRN**: `203712`
- **Resources**: Condition, Medication, MedicationOrder,
  MedicationRequest, MedicationStatement, Observation (Vitals), Patient

### Olivia Roberts
- **FHIR id**: `eh2xYHuzl9nkSFVvV3osUHg3`
- **External id**: `Z6131`
- **MRN**: `203715`
- **Resources**: Binary, Condition, Device, DocumentReference, Patient

### Warren McGinnis
- **FHIR id**: `e0w0LEDCYtfckT6N.CkJKCw3`
- **External id**: `Z6126`
- **MRN**: `203710`
- **Resources**: AllergyIntolerance, Binary, Condition, DiagnosticReport,
  DocumentReference, Observation (Labs), Observation (Vitals), Patient,
  Procedure

## Which patients have DiagnosticReport data

Of the seven above, only **Camila Lopez** and **Warren McGinnis** list
`DiagnosticReport` in their resource set — the two worth using to
exercise `/pathology`'s report/observation rendering. The other five are
scoped to different resource types (conditions, medications,
immunizations, documents, ...) and won't show anything under "Pathology
& genomics reports" on their patient page, even though the
patient/MRN/id lookup itself will still succeed.
