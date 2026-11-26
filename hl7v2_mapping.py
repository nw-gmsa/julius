"""Translating FHIR validation issues back to the HL7 v2 message they
were converted from.

An HL7 v2 upload is validated by the ESB's $validate
(FhirClient.validate_hl7v2_with_esb()): RIE.Transforms.HL7v2ToFHIRMessage
converts it to a FHIR Message, which RIE.FHIR.Validate checks. The
report is therefore about FHIR resources and paths
(`Specimen.type.coding[0].code`), which the sender of a v2 message can't
act on. This module turns each issue's location back into the v2
segment and field it came from (`SPM-4 Specimen Type`, line 7).

Two pieces of information:

- **Which segment** — the transform records in each resource's
  `meta.source` the segment(s) it was converted from, as
  `urn:hl7v2:<position>-<segment>[,...]` (position = the segment's place
  in the message, MSH = 1), but only for a $validate request (see
  TagSourceSegments in HL7v2ToFHIRMessage.cls). Without it (an ESB
  without that change, or a resource the transform made up itself) the
  segment is looked up by name in the message.
- **Which field** — FIELD_MAP below: per resource type, FHIR element
  path → v2 field, read off HL7v2ToFHIRMessage.cls's Get* methods
  (GetMSH, GetPID, GetPV1, GetNK1, GetORCO21, GetORCR01, GetOBX,
  GetOBXED, GetSPM, GetDG1, GetTaskO51, GetAndAddPractitionerRole).
  Elements the transform sets itself rather than from the message
  (categories, the NW GLH performer, references between resources, ...)
  map to CONVERSION: an issue there is about the conversion, not the
  sender's message. Keep this in step with the transform when it
  changes.
"""
import re

#: v2.5.1 field names for the fields FIELD_MAP uses.
FIELD_NAMES = {
    "MSH-3": "Sending Application", "MSH-4": "Sending Facility", "MSH-5": "Receiving Application",
    "MSH-6": "Receiving Facility", "MSH-7": "Date/Time of Message", "MSH-9": "Message Type",
    "MSH-10": "Message Control ID",
    "PID-2": "Patient ID", "PID-3": "Patient Identifier List", "PID-5": "Patient Name",
    "PID-7": "Date/Time of Birth", "PID-8": "Administrative Sex", "PID-11": "Patient Address",
    "PID-13": "Phone Number - Home", "PID-29": "Patient Death Date and Time", "PID-30": "Patient Death Indicator",
    "PV1-2": "Patient Class", "PV1-3": "Assigned Patient Location", "PV1-4": "Admission Type",
    "PV1-7": "Attending Doctor", "PV1-10": "Hospital Service", "PV1-17": "Admitting Doctor",
    "PV1-19": "Visit Number", "PV1-44": "Admit Date/Time", "PV1-45": "Discharge Date/Time",
    "NK1-2": "Name", "NK1-3": "Relationship", "NK1-4": "Address", "NK1-5": "Phone Number",
    "ORC-1": "Order Control", "ORC-2": "Placer Order Number", "ORC-3": "Filler Order Number",
    "ORC-4": "Placer Group Number", "ORC-5": "Order Status", "ORC-8": "Parent",
    "ORC-9": "Date/Time of Transaction", "ORC-12": "Ordering Provider", "ORC-21": "Ordering Facility Name",
    "OBR-2": "Placer Order Number", "OBR-3": "Filler Order Number", "OBR-4": "Universal Service Identifier",
    "OBR-6": "Requested Date/Time", "OBR-7": "Observation Date/Time", "OBR-16": "Ordering Provider",
    "OBR-20": "Filler Field 1", "OBR-22": "Results Rpt/Status Chng - Date/Time", "OBR-25": "Result Status",
    "OBR-29": "Parent", "OBR-31": "Reason for Study", "OBR-32": "Principal Result Interpreter",
    "OBR-33": "Assistant Result Interpreter", "OBR-34": "Technician", "OBR-35": "Transcriptionist",
    "OBX-2": "Value Type", "OBX-3": "Observation Identifier", "OBX-5": "Observation Value", "OBX-6": "Units",
    "OBX-7": "References Range", "OBX-8": "Abnormal Flags", "OBX-11": "Observation Result Status",
    "OBX-14": "Date/Time of the Observation", "OBX-17": "Observation Method",
    "NTE-3": "Comment",
    "SPM-2": "Specimen ID", "SPM-3": "Specimen Parent IDs", "SPM-4": "Specimen Type",
    "SPM-8": "Specimen Source Site", "SPM-17": "Specimen Collection Date/Time",
    "SPM-18": "Specimen Received Date/Time", "SPM-20": "Specimen Availability",
    "DG1-3": "Diagnosis Code", "DG1-5": "Diagnosis Date/Time",
}

#: Set by the transform itself, not taken from the message.
CONVERSION = "conversion"

#: Per resource type: (FHIR path prefix, v2 field(s) | CONVERSION). The
#: longest matching prefix wins; "" is the resource as a whole. Paths are
#: the issue's location with the resource type, list indexes and
#: .ofType() removed (Specimen.type.coding[0].code -> type.coding.code).
FIELD_MAP = {
    "Bundle": [("timestamp", "MSH-7"), ("identifier", "MSH-10"), ("type", CONVERSION), ("", "MSH")],
    "MessageHeader": [
        ("eventCoding", "MSH-9"), ("destination.endpoint", "MSH-5"), ("destination.receiver", "MSH-6"),
        ("destination", "MSH-5"), ("sender", "MSH-4"), ("source.endpoint", "MSH-3"), ("source", "MSH-3"),
        ("focus", CONVERSION), ("response", CONVERSION), ("", "MSH"),
    ],
    "Patient": [
        ("identifier", "PID-3"), ("name", "PID-5"), ("gender", "PID-8"), ("birthDate", "PID-7"),
        ("address", "PID-11"), ("telecom", "PID-13"), ("deceased", "PID-29"),
        ("managingOrganization", CONVERSION), ("", "PID"),
    ],
    "RelatedPerson": [
        ("name", "NK1-2"), ("relationship", "NK1-3"), ("address", "NK1-4"), ("telecom", "NK1-5"),
        ("patient", CONVERSION), ("", "NK1"),
    ],
    "Encounter": [
        ("identifier", "PV1-19"), ("class", "PV1-2"), ("location", "PV1-3"), ("serviceType", "PV1-10"),
        ("type", "PV1-4"), ("period.start", "PV1-44"), ("period.end", "PV1-45"), ("period", "PV1-44"),
        ("participant", "PV1-7"), ("status", CONVERSION), ("subject", CONVERSION), ("", "PV1"),
    ],
    "ServiceRequest": [
        ("identifier", "ORC-2"), ("requisition", "ORC-4"), ("status", "ORC-5"), ("authoredOn", "ORC-9"),
        ("requester", "ORC-12"), ("code", "OBR-4"), ("reasonCode", "OBR-31"), ("reasonReference", "DG1-3"),
        ("basedOn", "ORC-8"), ("note", "NTE-3"), ("specimen", "SPM"),
        ("intent", CONVERSION), ("category", CONVERSION), ("subject", CONVERSION), ("encounter", CONVERSION),
        ("", "ORC"),
    ],
    "DiagnosticReport": [
        ("identifier", "OBR-3"), ("status", "OBR-25"), ("code", "OBR-4"), ("effective", "OBR-7"),
        ("issued", "OBR-22"), ("resultsInterpreter", "OBR-32"), ("conclusionCode", "DG1-3"),
        ("basedOn", "ORC-2"), ("specimen", "SPM"), ("result", "OBX"), ("presentedForm", "OBX-5"),
        ("performer", CONVERSION), ("category", CONVERSION), ("subject", CONVERSION),
        ("encounter", CONVERSION), ("", "OBR"),
    ],
    "Observation": [
        ("code", "OBX-3"), ("value", "OBX-5"), ("component", "OBX-5"), ("dataAbsentReason", "OBX-5"),
        ("referenceRange", "OBX-7"), ("interpretation", "OBX-8"), ("status", "OBX-11"),
        ("effective", "OBX-14"), ("method", "OBX-17"), ("note", "NTE-3"), ("specimen", "SPM"),
        ("identifier", "OBR-3"), ("hasMember", "OBX"),
        ("performer", CONVERSION), ("category", CONVERSION), ("subject", CONVERSION),
        ("derivedFrom", CONVERSION), ("", "OBX"),
    ],
    "DocumentReference": [
        ("content", "OBX-5"), ("type", "OBX-3"), ("date", "OBX-14"), ("description", "OBX-3"),
        ("status", CONVERSION), ("subject", CONVERSION), ("context", CONVERSION), ("", "OBX"),
    ],
    "Specimen": [
        ("identifier", "SPM-2"), ("accessionIdentifier", "SPM-3"), ("type", "SPM-4"),
        ("collection.bodySite", "SPM-8"), ("collection", "SPM-17"), ("receivedTime", "SPM-18"),
        ("status", "SPM-20"), ("subject", CONVERSION), ("", "SPM"),
    ],
    "Condition": [("code", "DG1-3"), ("onset", "DG1-5"), ("subject", CONVERSION), ("", "DG1")],
    "PractitionerRole": [
        ("practitioner", "ORC-12"), ("organization", "ORC-21"), ("identifier", "ORC-12"), ("", "ORC"),
    ],
    "Practitioner": [("", "ORC-12")],
    "Organization": [("", "ORC-21")],
    "Task": [
        ("status", "ORC-5"), ("businessStatus", "ORC-5"), ("intent", CONVERSION), ("code", "ORC-1"),
        ("identifier", "ORC-3"), ("basedOn", "ORC-2"), ("output", "OBR"), ("", "ORC"),
    ],
    "Procedure": [("", CONVERSION)],
}

_SOURCE_RE = re.compile(r"(\d+)-([A-Z][A-Z0-9]{2})")
_ENTRY_PATH_RE = re.compile(r"^Bundle\.entry\[(\d+)\](?:\.resource(?:\.ofType\(\w+\))?)?\.?(.*)$")


def segments(message):
    """The message's segments in order (position 1 = MSH), split on any
    line ending — the same positions the transform's SourceSegments uses."""
    return [s for s in re.split(r"\r\n|\n|\r", (message or "").strip("﻿ \t\r\n")) if s.strip()]


def normalise(message):
    """The message with \\r segment separators, as sent to the ESB."""
    return "\r".join(segments(message)) + "\r"


def message_event(message):
    """MSH-9 (e.g. "OML^O21^OML_O21") of a v2 message, or None."""
    segs = segments(message)
    if not segs or not segs[0].startswith("MSH"):
        return None
    fields = segs[0].split(segs[0][3] if len(segs[0]) > 3 else "|")
    return fields[8] if len(fields) > 8 else None


def resource_sources(resource):
    """[(position, segment name), ...] from a resource's meta.source."""
    source = ((resource or {}).get("meta") or {}).get("source") or ""
    if not source.startswith("urn:hl7v2:"):
        return []
    return [(int(pos), name) for pos, name in _SOURCE_RE.findall(source[len("urn:hl7v2:"):])]


def _clean_path(path):
    path = re.sub(r"\.ofType\(\w+\)", "", path or "")
    path = re.sub(r"\[\d+\]", "", path)
    return path.strip(".")


def _rule(resource_type, path):
    best = None
    for prefix, target in FIELD_MAP.get(resource_type, []):
        if prefix == "" or path == prefix or path.startswith(prefix + ".") or (
                prefix in ("value", "effective", "onset", "deceased") and path.startswith(prefix)):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, target)
    return best[1] if best else None


def _line_for(segment_name, sources, segs):
    """Position of `segment_name` for a resource: one of its own source
    segments if it has one by that name, else the nearest such segment in
    the message (one Patient, one PID — usually unambiguous)."""
    for pos, name in sources:
        if name == segment_name:
            return pos
    candidates = [i for i, s in enumerate(segs, 1) if s[:3] == segment_name]
    if not candidates:
        return None
    anchor = sources[0][0] if sources else 0
    return min(candidates, key=lambda i: abs(i - anchor))


def locate(resource, path, segs):
    """The v2 location of an issue on `resource` at FHIR `path` (relative
    to the resource): {text, field, segment, line, conversion}."""
    resource = resource or {}
    rtype = resource.get("resourceType") or ""
    sources = resource_sources(resource)
    target = _rule(rtype, _clean_path(path))
    if target == CONVERSION:
        seg = sources[0] if sources else None
        return {"text": "Added by the HL7 v2 → FHIR conversion", "field": None,
                "segment": seg[1] if seg else None, "line": seg[0] if seg else None, "conversion": True}
    if target is None:
        if not sources:
            return {"text": f"{rtype} — not mapped to HL7 v2", "field": None, "segment": None, "line": None,
                    "conversion": False}
        pos, name = sources[0]
        return {"text": name, "field": None, "segment": name, "line": pos, "conversion": False}
    seg_name = target.split("-")[0]
    field = target if "-" in target else None
    line = _line_for(seg_name, sources, segs)
    text = f"{field} {FIELD_NAMES.get(field, '')}".strip() if field else seg_name
    return {"text": text, "field": field, "segment": seg_name, "line": line, "conversion": False}


def _resource_and_path(job, issue, bundle):
    """(resource, path relative to it) for an issue: a Bundle entry when
    the issue is inside one, the Bundle itself otherwise, or the resource
    a follow-up result validated on its own."""
    location = issue.get("location") or ""
    if job["key"] == "all":
        m = _ENTRY_PATH_RE.match(location)
        if m:
            entries = (bundle or {}).get("entry") or []
            i = int(m.group(1))
            return ((entries[i].get("resource") if i < len(entries) else None) or {}), m.group(2)
        return bundle or {}, re.sub(r"^Bundle\.?", "", location)
    resource = job.get("resource") or {}
    return resource, re.sub(r"^" + re.escape(resource.get("resourceType") or "") + r"\.?", "", location)


def annotate(jobs, bundle, message, profile_mismatch=None, find_entry=None):
    """Adds `v2` (locate()'s dict) to every issue of every validated
    resource. An "Unable to find a profile match" issue is about the
    referenced resource, so it's located at that resource's segment(s)
    rather than at MessageHeader.focus (`profile_mismatch`/`find_entry`:
    fhir_validator's helpers, passed in to avoid a circular import)."""
    segs = segments(message)
    entries = (bundle or {}).get("entry") or []
    for job in jobs:
        for issue in job.get("issues") or []:
            mismatch = profile_mismatch(issue) if profile_mismatch else None
            index = find_entry(bundle, mismatch[0]) if mismatch and find_entry and bundle else None
            if index is not None and index < len(entries):
                issue["v2"] = locate(entries[index].get("resource") or {}, "", segs)
            else:
                resource, path = _resource_and_path(job, issue, bundle)
                issue["v2"] = locate(resource, path, segs)
