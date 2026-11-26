"""FHIR validation for /testing/send-message. Two backends, picked by
FHIR_VALIDATION_BACKEND (read fresh on every call):

- "esb" (default) — POST to the ESB's $validate (ESB_BASE_URL +
  /ESB/$validate, FhirClient.validate_with_esb()): RIE.FHIR.Validate runs
  the HL7 FHIR Validator on the ESB servers. Profile, -bundle rule,
  terminology server and ignored issues are that component's own
  settings, so this app's profile / -bundle / terminology choices don't
  apply. Only message Bundles (O21/O51/R01/R32) and single non-Bundle
  resources (against the NW profile named after their type) are accepted.
- "local" — the HL7 FHIR Validator CLI (validator_cli.jar) as a
  subprocess inside this app (run_validator()), the same tool and flags
  the NW GMSA IG's own testing page uses
  (https://nw-gmsa.github.io/en/testing-resources.html). Kept so the app
  can revert to it.

Configuration for the local backend (environment variables, read fresh
on every call):

- FHIR_VALIDATOR_JAR    path to validator_cli.jar
                        (default data/validator/validator_cli.jar —
                        git-ignored; download it from
                        VALIDATOR_DOWNLOAD_URL)
- FHIR_VALIDATOR_JAVA   java executable (default "java"; Java 17+)
- FHIR_VALIDATOR_JAVA_OPTS  extra JVM options, e.g. "-Xmx4g"
- FHIR_VALIDATOR_TX_URL an extra terminology server offered alongside
                        tx.fhir.org / none
- FHIR_VALIDATOR_TIMEOUT seconds per validator run (default 600)
- FHIR_VALIDATOR_CACHE_DIR where IG packages are cached
                        (default data/validator)
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
VALIDATOR_DOWNLOAD_URL = ("https://github.com/hapifhir/org.hl7.fhir.core/releases/latest/"
                          "download/validator_cli.jar")
FHIR_VERSION = "4.0.1"

#: IG packages a resource can be validated against. `package_url` is
#: where the .tgz is downloaded from; it's cached under the cache dir and
#: passed to the validator as -ig <local path> (the validator then pulls
#: the package's own dependencies — UK Core, EU Laboratory, ... — from
#: the FHIR package registry itself). The NW GMSA package isn't on the
#: registry (hence the GitHub Pages URL) and is rebuilt often, so it's
#: re-downloaded once it's older than `max_age_hours`; the published
#: Genomics Reporting release never changes.
IGS = {
    "nw-gmsa": {
        "label": "NW GMSA (fhir.nwgenomics.nhs.uk)",
        "package_url": "https://nw-gmsa.github.io/package.tgz",
        "filename": "fhir.nwgenomics.nhs.uk.tgz",
        "max_age_hours": 24,
    },
    "genomics-reporting": {
        "label": "HL7 FHIR Genomics Reporting 3.0.0",
        "package_url": "https://packages.fhir.org/hl7.fhir.uv.genomics-reporting/3.0.0",
        "filename": "hl7.fhir.uv.genomics-reporting#3.0.0.tgz",
        "max_age_hours": None,
    },
}
DEFAULT_IG = "nw-gmsa"

#: Terminology server choices: value passed to -tx. tx.fhir.org is the
#: validator's own default (and what the IG's testing page uses);
#: "n/a" turns terminology checking off (much faster, but codes aren't
#: checked). The NHS England Ontology Server can't be used here — its API
#: security isn't supported by the validator (per the IG's testing page).
TX_FHIR_ORG = "http://tx.fhir.org"
TX_NONE = "n/a"


def terminology_options():
    options = [(TX_FHIR_ORG, "tx.fhir.org (HL7 public terminology server)"),
               (TX_NONE, "None — skip terminology checks (faster)")]
    extra = os.environ.get("FHIR_VALIDATOR_TX_URL")
    if extra:
        options.insert(0, (extra, f"{extra} (configured)"))
    return options


def _cache_dir():
    path = os.environ.get("FHIR_VALIDATOR_CACHE_DIR") or os.path.join(HERE, "data", "validator")
    os.makedirs(path, exist_ok=True)
    return path


def jar_path():
    return os.environ.get("FHIR_VALIDATOR_JAR") or os.path.join(HERE, "data", "validator", "validator_cli.jar")


def java_path():
    return os.environ.get("FHIR_VALIDATOR_JAVA") or "java"


BACKEND_ESB = "esb"
BACKEND_LOCAL = "local"


def backend():
    """FHIR_VALIDATION_BACKEND: "esb" (default) or "local"."""
    value = (os.environ.get("FHIR_VALIDATION_BACKEND") or BACKEND_ESB).strip().lower()
    return BACKEND_LOCAL if value == BACKEND_LOCAL else BACKEND_ESB


def setup_problems():
    """List of human-readable reasons validation can't run (empty = ready)."""
    if backend() == BACKEND_ESB:
        return []
    return _local_setup_problems()


def _local_setup_problems():
    problems = []
    if not shutil.which(java_path()):
        problems.append(f"Java not found ('{java_path()}') — install Java 17+ or set FHIR_VALIDATOR_JAVA.")
    if not os.path.isfile(jar_path()):
        problems.append(f"Validator not found at {jar_path()} — download validator_cli.jar from "
                        f"{VALIDATOR_DOWNLOAD_URL} or set FHIR_VALIDATOR_JAR.")
    return problems


# --- IG packages and their profiles ----------------------------------------

_package_lock = threading.Lock()
_profile_cache = {}  # ig_key -> (mtime, {resourceType: [(url, title), ...]})


def package_path(ig_key):
    """Local path of the IG's package .tgz, downloading it if missing or
    stale. A failed refresh falls back to the cached copy if there is one."""
    ig = IGS[ig_key]
    path = os.path.join(_cache_dir(), ig["filename"])
    with _package_lock:
        fresh = os.path.isfile(path) and (
            ig["max_age_hours"] is None
            or time.time() - os.path.getmtime(path) < ig["max_age_hours"] * 3600)
        if fresh:
            return path
        try:
            resp = requests.get(ig["package_url"], timeout=60)
            resp.raise_for_status()
            if resp.content[:2] != b"\x1f\x8b":
                raise ValueError(f"{ig['package_url']} did not return a .tgz package")
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(resp.content)
            os.replace(tmp, path)
        except Exception:
            if not os.path.isfile(path):
                raise
        return path


def profiles(ig_key):
    """{resourceType: [(profile url, title), ...]} for every resource
    profile (StructureDefinition kind=resource, derivation=constraint) in
    the IG's package, sorted by title."""
    path = package_path(ig_key)
    mtime = os.path.getmtime(path)
    cached = _profile_cache.get(ig_key)
    if cached and cached[0] == mtime:
        return cached[1]
    by_type = {}
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.rsplit("/", 1)[-1]
            if not (member.isfile() and name.startswith("StructureDefinition-") and name.endswith(".json")):
                continue
            try:
                sd = json.load(tar.extractfile(member))
            except ValueError:
                continue
            if sd.get("kind") != "resource" or sd.get("derivation") != "constraint":
                continue
            by_type.setdefault(sd.get("type"), []).append(
                (sd.get("url"), sd.get("title") or sd.get("name") or sd.get("url")))
    for items in by_type.values():
        items.sort(key=lambda p: p[1].lower())
    _profile_cache[ig_key] = (mtime, by_type)
    return by_type


def profile_choices(resource_type):
    """Grouped choices for one resource type, for a <select>:
    [(ig label, [(value, label), ...]), ...]. Values are "<ig>|<profile>",
    profile empty meaning base resource + whatever meta.profile claims.
    An IG whose package can't be fetched is listed with only its base
    option (the error shows when validation actually runs)."""
    groups = []
    for ig_key, ig in IGS.items():
        options = [(f"{ig_key}|", f"Base {resource_type} (+ meta.profile) — {ig['label']} package")]
        try:
            options += [(f"{ig_key}|{url}", f"{title} — {url}")
                        for url, title in profiles(ig_key).get(resource_type, [])]
        except Exception:
            pass
        groups.append((ig["label"], options))
    return groups


#: Resource types whose default profile is fixed, regardless of
#: meta.profile or what an "Unable to find a profile match" suggested.
TYPE_DEFAULT_PROFILES = {
    "Observation": "https://fhir.nwgenomics.nhs.uk/StructureDefinition/Observation",
}


def default_choice(resource):
    """Best default "<ig>|<profile>" for a resource: a meta.profile the
    NW GMSA package defines, then the NW GMSA profile named after the
    resource type (BundleMessage/BundleDocument for a Bundle), else the
    base resource with the NW GMSA package loaded."""
    resource_type = resource.get("resourceType")
    try:
        known = {url for url, _ in profiles(DEFAULT_IG).get(resource_type, [])}
    except Exception:
        known = set()
    # Observations always start on the general NW Observation profile
    # (the validator still checks any meta.profile they declare as well).
    fixed = TYPE_DEFAULT_PROFILES.get(resource_type)
    if fixed in known:
        return f"{DEFAULT_IG}|{fixed}"
    for url in (resource.get("meta") or {}).get("profile") or []:
        if url in known:
            return f"{DEFAULT_IG}|{url}"
    base = "https://fhir.nwgenomics.nhs.uk/StructureDefinition/"
    if resource_type == "Bundle":
        guess = {"message": "BundleMessage", "document": "BundleDocument"}.get(resource.get("type"))
    else:
        guess = resource_type
    if guess and base + guess in known:
        return f"{DEFAULT_IG}|{base}{guess}"
    return f"{DEFAULT_IG}|"


def parse_choice(value):
    ig_key, _, profile = (value or "").partition("|")
    if ig_key not in IGS:
        ig_key, profile = DEFAULT_IG, ""
    return ig_key, profile or None


# --- Targets ----------------------------------------------------------------

def pretty(resource):
    """The exact text handed to the validator — indented, so the line
    numbers it reports point at a real line, and the same text the
    results page shows when an issue is clicked."""
    return json.dumps(resource, indent=2, ensure_ascii=False)


def canonical(resource):
    """Order-independent text of a resource, to tell whether it changed."""
    return json.dumps(resource, sort_keys=True, separators=(",", ":"))


def _concept_text(cc):
    if not isinstance(cc, dict):
        return None
    if cc.get("text"):
        return cc["text"]
    for coding in cc.get("coding") or []:
        if coding.get("display") or coding.get("code"):
            return coding.get("display") or coding.get("code")
    return None


def _value_text(res):
    if "valueCodeableConcept" in res:
        return _concept_text(res["valueCodeableConcept"])
    q = res.get("valueQuantity")
    if isinstance(q, dict):
        return " ".join(str(x) for x in (q.get("value"), q.get("unit") or q.get("code")) if x is not None)
    for key in ("valueString", "valueDateTime", "valueBoolean", "valueInteger", "valueDate"):
        if key in res:
            return str(res[key])
    return None


def describe_resource(res):
    """Short human label for a resource so several of the same type can be
    told apart: "Observation abc — Consanguinity = No"."""
    parts = [res.get("resourceType") or "Resource"]
    if res.get("id"):
        parts.append(res["id"])
    what = None
    for key in ("code", "type", "eventCoding"):
        what = _concept_text(res.get(key)) if key != "eventCoding" else (res.get(key) or {}).get("display")
        if what:
            break
    if not what and res.get("name"):
        name = res["name"][0] if isinstance(res["name"], list) else res["name"]
        what = (name.get("text") or " ".join((name.get("given") or []) + [name.get("family") or ""]).strip()
                if isinstance(name, dict) else str(name))
    if not what and isinstance(res.get("practitioner"), dict):
        what = res["practitioner"].get("display")
    label = " ".join(parts)
    if what and len(what) > 80:
        what = what[:77] + "…"
    if what:
        value = _value_text(res)
        label += f" — {what}" + (f" = {value}" if value else "")
    return label


#: Validator -bundle rules for message Bundles, as on the IG's testing
#: page: validate the message's focal resource (the first of its type in
#: the Bundle) against the NW profile as well as the Bundle itself. Keyed
#: by MessageHeader.eventCoding.code (HL7 v2 event).
BUNDLE_RULES = {
    "O21": {"rule": "ServiceRequest:0", "event": "O21",
            "profile": "https://fhir.nwgenomics.nhs.uk/StructureDefinition/ServiceRequest",
            "label": "O21 laboratory order"},
    "R01": {"rule": "DiagnosticReport:0", "event": "R01",
            "profile": "https://fhir.nwgenomics.nhs.uk/StructureDefinition/LaboratoryReport",
            "label": "R01 report — Laboratory Report"},
    "R01-TestResults": {"rule": "DiagnosticReport:0", "event": "R01",
                        "profile": "https://fhir.nwgenomics.nhs.uk/StructureDefinition/TestResults",
                        "label": "R01 report — Test Results"},
}


def message_event_code(bundle):
    """MessageHeader.eventCoding.code of a message Bundle, else None."""
    for entry in bundle.get("entry") or []:
        res = entry.get("resource") or {}
        if res.get("resourceType") == "MessageHeader":
            return (res.get("eventCoding") or {}).get("code")
    return None


def bundle_rule_choices():
    return [(key, f"{r['label']}: -bundle {r['rule']} {r['profile']}") for key, r in BUNDLE_RULES.items()]


def default_bundle_rule(bundle):
    """The first rule for the message's event (dict order), else none —
    so R01 defaults to Laboratory Report, with Test Results offered."""
    code = message_event_code(bundle)
    return next((key for key, r in BUNDLE_RULES.items() if r["event"] == code), "")


def validation_targets(resource):
    """What can be validated in an uploaded resource: the resource itself
    (key "all"), plus — for a Bundle — each entry's resource on its own
    (key "entry-<i>"). Each is {key, label, resource_type, resource,
    default}."""
    targets = [{
        "key": "all",
        "label": (f"Entire Bundle ({resource.get('type') or 'no type'}, "
                  f"{len(resource.get('entry') or [])} entries)"
                  if resource.get("resourceType") == "Bundle"
                  else describe_resource(resource)),
        "resource_type": resource.get("resourceType"),
        "resource": resource,
    }]
    if resource.get("resourceType") == "Bundle":
        for i, entry in enumerate(resource.get("entry") or []):
            res = entry.get("resource")
            if not isinstance(res, dict) or not res.get("resourceType"):
                continue
            targets.append({
                "key": f"entry-{i}",
                "label": f"entry[{i}] {describe_resource(res)}",
                "resource_type": res["resourceType"],
                "resource": res,
            })
    for t in targets:
        t["default"] = default_choice(t["resource"])
    return targets


# --- Running the validator -------------------------------------------------

_run_slots = threading.Semaphore(int(os.environ.get("FHIR_VALIDATOR_MAX_CONCURRENT", "1")))


def _issue_rows(outcome):
    rows = []
    for issue in (outcome or {}).get("issue") or []:
        line = None
        for ext in issue.get("extension") or []:
            if ext.get("url", "").endswith("operationoutcome-issue-line"):
                line = ext.get("valueInteger")
        rows.append({
            "severity": issue.get("severity") or "",
            "code": issue.get("code") or "",
            "location": ((issue.get("expression") or issue.get("location") or [""])[0]),
            "line": line,
            "message": (issue.get("details") or {}).get("text") or issue.get("diagnostics") or "",
        })
    order = {"fatal": 0, "error": 1, "warning": 2, "information": 3}
    rows.sort(key=lambda r: order.get(r["severity"], 9))
    return rows


def _outcomes_by_file(output):
    """Validator -output is one OperationOutcome for a single file, a
    Bundle of them for several; each names its file in an
    operationoutcome-file extension."""
    if output.get("resourceType") == "OperationOutcome":
        outcomes = [output]
    else:
        outcomes = [e.get("resource") for e in output.get("entry") or [] if e.get("resource")]
    by_file = {}
    for i, oo in enumerate(outcomes):
        name = None
        for ext in oo.get("extension") or []:
            if ext.get("url", "").endswith("operationoutcome-file"):
                name = os.path.basename(ext.get("valueString") or "")
        by_file[name or i] = oo
    return by_file


def run_validator(resources, ig_key, profile, tx, bundle_rule=None):
    """Validates each resource in `resources` (list of dicts) in one
    validator run against `profile` (or just the base resource and its
    meta.profile when None) with `ig_key`'s package loaded. Returns
    (list of OperationOutcome per resource, console log text)."""
    problems = _local_setup_problems()
    if problems:
        raise RuntimeError(" ".join(problems))
    with tempfile.TemporaryDirectory(prefix="fhir-validate-") as tmp:
        files = []
        for i, res in enumerate(resources):
            name = f"{i:03d}-{res.get('resourceType', 'resource')}.json"
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write(pretty(res))
            files.append(name)
        out = os.path.join(tmp, "results.json")
        cmd = [java_path(), *shlex.split(os.environ.get("FHIR_VALIDATOR_JAVA_OPTS", "")),
               "-jar", jar_path(), *[os.path.join(tmp, f) for f in files],
               "-version", FHIR_VERSION, "-ig", package_path(ig_key), "-tx", tx or TX_FHIR_ORG]
        if tx != TX_NONE:
            cmd += ["-sct", "uk"]  # the IG is built against the UK SNOMED CT edition
        if profile:
            cmd += ["-profile", profile]
        if bundle_rule in BUNDLE_RULES:
            rule = BUNDLE_RULES[bundle_rule]
            cmd += ["-bundle", rule["rule"], rule["profile"]]
        cmd += ["-output", out]
        with _run_slots:
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                                  timeout=int(os.environ.get("FHIR_VALIDATOR_TIMEOUT", "600")))
        log = (proc.stdout or "") + (proc.stderr or "")
        # The validator exits non-zero when it finds errors, so the exit
        # code alone doesn't mean it failed — only a missing output does.
        if not os.path.isfile(out):
            tail = "\n".join(log.strip().splitlines()[-15:])
            raise RuntimeError(f"Validator produced no results (exit {proc.returncode}):\n{tail}")
        with open(out, encoding="utf-8") as f:
            by_file = _outcomes_by_file(json.load(f))
    outcomes = [by_file.get(name, by_file.get(i)) for i, name in enumerate(files)]
    return outcomes, log


_ENTRY_RE = re.compile(r"^Bundle\.entry\[(\d+)\]")


def _annotate_bundle_entries(job):
    """For a whole-Bundle validation, adds `entry` (index) and
    `entry_label` to each issue whose location is inside an entry, so an
    issue on one of many Observations says which one."""
    resource = job["resource"]
    if resource.get("resourceType") != "Bundle":
        return
    entries = resource.get("entry") or []
    for issue in job.get("issues") or []:
        m = _ENTRY_RE.match(issue["location"] or "")
        if m and int(m.group(1)) < len(entries):
            i = int(m.group(1))
            res = entries[i].get("resource") or {}
            issue["entry"] = i
            issue["entry_label"] = f"entry[{i}] {describe_resource(res)}"
            issue["entry_type"] = res.get("resourceType")


# --- The ESB's $validate ---------------------------------------------------

NW_PROFILE_BASE = "https://fhir.nwgenomics.nhs.uk/StructureDefinition/"

#: What RIE.FHIR.Validate validates a message's focus resource against
#: (its FocusResourceType()/FocusProfile() and the *Profile settings'
#: defaults) — shown on the report only; the ESB decides.
ESB_EVENT_FOCUS = {
    "O21": ("ServiceRequest", NW_PROFILE_BASE + "ServiceRequest"),
    "O51": ("Task", NW_PROFILE_BASE + "Task"),
    "R01": ("DiagnosticReport", NW_PROFILE_BASE + "DiagnosticReport"),
    "R32": ("DiagnosticReport", NW_PROFILE_BASE + "DiagnosticReport"),
}

ESB_IG_LABEL = "ESB $validate"


def esb_validation_plan(resource):
    """(profile, bundle_rule_text) the ESB is expected to use for
    `resource`, for the report — RIE.FHIR.Validate's defaults, which may
    differ if its settings were changed."""
    if resource.get("resourceType") == "Bundle":
        if resource.get("type") != "message":
            return "not accepted (only message Bundles)", None
        event = message_event_code(resource)
        focus = ESB_EVENT_FOCUS.get(event or "")
        if not focus:
            return f"not accepted (event {event or 'unknown'})", None
        return NW_PROFILE_BASE + "BundleMessage", f"-bundle {focus[0]}:0 {focus[1]}"
    return NW_PROFILE_BASE + resource.get("resourceType", ""), None


def _validate_esb(jobs):
    from fhir_client import FhirClient  # fhir_client doesn't import this module
    timeout = int(os.environ.get("FHIR_VALIDATOR_TIMEOUT", "600"))
    for job in jobs:
        started = time.time()
        try:
            outcome = FhirClient.validate_with_esb(pretty(job["resource"]), timeout=timeout)
            issues = _issue_rows(outcome)
            if (len(issues) == 1 and issues[0]["severity"] == "information"
                    and "validation disabled" in issues[0]["message"].lower()):
                job["error"], job["issues"] = ("FHIR validation is disabled on the ESB "
                                               "(FHIR.Validate's FHIRValidatorEnabled setting)."), []
            else:
                job["issues"] = issues
            print(f"[fhir_validator] ESB $validate {job['label']}: {len(issues)} issue(s) "
                  f"in {time.time() - started:.0f}s", flush=True)
        except requests.Timeout:
            job["error"], job["issues"] = "The ESB's $validate timed out.", []
        except Exception as e:
            print(f"[fhir_validator] ESB $validate {job['label']}: failed — {e}", flush=True)
            job["error"], job["issues"] = str(e), []


def validate(jobs, tx):
    """`jobs`: list of {label, resource, ig, profile}. Validates each via
    the configured backend() and returns the jobs, each with `issues`,
    `counts` and `error` added. `tx` only applies to the local backend."""
    if backend() == BACKEND_ESB:
        _validate_esb(jobs)
    else:
        _validate_local(jobs, tx)
    for job in jobs:
        job["json"] = pretty(job["resource"])
        _annotate_bundle_entries(job)
        counts = {"error": 0, "warning": 0, "information": 0}
        for issue in job.get("issues") or []:
            sev = "error" if issue["severity"] == "fatal" else issue["severity"]
            counts[sev] = counts.get(sev, 0) + 1
        job["counts"] = counts
    return jobs


def _validate_local(jobs, tx):
    """Local backend: runs one validator per distinct (ig, profile) — each
    JVM start + package load takes tens of seconds, so resources sharing a
    profile go together."""
    groups = {}
    for job in jobs:
        groups.setdefault((job["ig"], job["profile"], job.get("bundle_rule") or None), []).append(job)
    for (ig_key, profile, bundle_rule), group in groups.items():
        started = time.time()
        try:
            outcomes, log = run_validator([j["resource"] for j in group], ig_key, profile, tx, bundle_rule)
            tail = "\n".join(log.strip().splitlines()[-60:])
            print(f"[fhir_validator] {ig_key} {profile or '(base)'}: {len(group)} resource(s) "
                  f"in {time.time() - started:.0f}s", flush=True)
            for job, oo in zip(group, outcomes):
                job["log"] = tail
                if oo is None:
                    job["error"] = "No result returned for this resource."
                    job["issues"] = []
                else:
                    job["issues"] = _issue_rows(oo)
        except subprocess.TimeoutExpired:
            print(f"[fhir_validator] {ig_key} {profile or '(base)'}: timed out", flush=True)
            for job in group:
                job["error"], job["issues"] = "Validator timed out.", []
        except Exception as e:
            print(f"[fhir_validator] {ig_key} {profile or '(base)'}: failed — {e}", flush=True)
            for job in group:
                job["error"], job["issues"] = str(e), []


# --- "Unable to find a profile match" ---------------------------------------
#
# When a reference's targetProfile isn't met, the validator only says
# "Unable to find a profile match for <ref> among choices: <profiles>" —
# not *why*. annotate_profile_mismatches() finds <ref> in the uploaded
# Bundle so the report can offer to validate that one resource against
# the listed profile (the user chooses; nothing is run automatically).

_PROFILE_MATCH_RE = re.compile(
    r"Unable to find a (?:profile )?match for (?:profile )?(?P<ref>\S+?)(?: \(by type\))?"
    r" among choices:?\s*(?P<choices>.+)", re.IGNORECASE | re.DOTALL)
_URL_RE = re.compile(r"https?://[^\s,;'\"()\[\]]+")


def _profile_mismatch(issue):
    m = _PROFILE_MATCH_RE.search(issue.get("message") or "")
    if not m:
        return None
    profiles = [u.rstrip(".") for u in _URL_RE.findall(m.group("choices"))]
    return (m.group("ref").strip("'\"`"), profiles) if profiles else None


def _find_entry(bundle, ref):
    """Index of the Bundle entry `ref` points at (fullUrl, Type/id, an
    absolute URL ending Type/id, or Bundle.entry[n]), else None."""
    entries = (bundle or {}).get("entry") or []
    m = _ENTRY_RE.match(ref)
    if m and int(m.group(1)) < len(entries):
        return int(m.group(1))
    ref = ref.split("/*")[0]
    for i, entry in enumerate(entries):
        if entry.get("fullUrl") and entry["fullUrl"] == ref:
            return i
    for i, entry in enumerate(entries):
        res = entry.get("resource") or {}
        type_id = f"{res.get('resourceType')}/{res.get('id')}"
        if res.get("id") and (ref == type_id or ref.endswith("/" + type_id)):
            return i
    return None


def annotate_profile_mismatches(jobs, bundle):
    """Adds `suggest` ([{key, choice, label}] — the referenced entry and
    each listed profile, ready for the "validate this resource" button)
    or `mismatch_note` to every "Unable to find a profile match" issue."""
    entries = (bundle or {}).get("entry") or []
    for job in jobs:
        for issue in job.get("issues") or []:
            mismatch = _profile_mismatch(issue)
            if not mismatch:
                continue
            ref, profiles = mismatch
            index = _find_entry(bundle, ref) if bundle else None
            if index is None:
                issue["mismatch_note"] = f"Referenced resource {ref} isn't in the uploaded Bundle."
                continue
            res = entries[index].get("resource") or {}
            # The NW package depends on Genomics Reporting, so loading it
            # resolves either IG's profiles.
            issue["suggest"] = [{"key": f"entry-{index}", "choice": f"{DEFAULT_IG}|{url}",
                                 "label": f"entry[{index}] {describe_resource(res)}", "profile": url}
                                for url in profiles]


def suggestions(jobs):
    """Unique (key, choice) suggestions across every issue, in order."""
    seen, out = set(), []
    for job in jobs:
        for issue in job.get("issues") or []:
            for sug in issue.get("suggest") or []:
                if (sug["key"], sug["choice"]) not in seen:
                    seen.add((sug["key"], sug["choice"]))
                    out.append(sug)
    return out


#: Resource types only the first suggested entry of which is validated
#: automatically — a message usually carries many Observations built the
#: same way, so they tend to share one problem.
FOLLOW_UP_FIRST_ONLY_TYPES = {"Observation"}


def follow_up_targets(jobs, bundle):
    """Entries to validate automatically once a Bundle's report is in:
    every entry named in an "Unable to find a profile match" issue (after
    annotate_profile_mismatches()), in Bundle order, against its type's
    default profile (default_choice() — the NW Observation profile for an
    Observation) — but only the first of each FOLLOW_UP_FIRST_ONLY_TYPES
    type. Entries already validated are skipped. Returns a list of
    {key, label, resource, choice, represents} — `represents` lists the
    labels of the other entries of a first-only type that were named too
    but not validated (the report explains the one stands in for them)."""
    if not isinstance(bundle, dict):
        return []
    entries = bundle.get("entry") or []
    done = {job["key"] for job in jobs}
    indexes = sorted({int(sug["key"].split("-", 1)[1]) for sug in suggestions(jobs)})
    picks, first_of_type = [], {}
    for i in indexes:
        key = f"entry-{i}"
        res = (entries[i].get("resource") if i < len(entries) else None) or {}
        res_type = res.get("resourceType")
        if not res_type:
            continue
        label = f"entry[{i}] {describe_resource(res)}"
        if res_type in FOLLOW_UP_FIRST_ONLY_TYPES:
            if res_type in first_of_type:
                if first_of_type[res_type] is not None:
                    first_of_type[res_type]["represents"].append(label)
                continue
            first_of_type[res_type] = None
        if key in done:
            continue
        pick = {"key": key, "label": label, "resource": res, "choice": default_choice(res),
                "represents": []}
        if res_type in FOLLOW_UP_FIRST_ONLY_TYPES:
            first_of_type[res_type] = pick
        picks.append(pick)
    return picks


# --- Human-readable report ---------------------------------------------------

#: Issues left out of the report by default (case-insensitive substring
#: of the message), one per line in the report's "Ignore" box. dom-6 is
#: "A resource should have narrative for robust management" — not
#: something these messages are expected to carry. This list is a
#: shared requirement, documented in nw-gmsa-use-cases DataSecurity.md
#: ("Ignored validation issues"): keep it the same as RIE.FHIR.Validate's
#: IgnoredIssues setting, which strips the same issues from RIE's
#: OperationOutcomes.
DEFAULT_IGNORED_ISSUES = ["Constraint failed: dom-6"]
_SEVERITY_ORDER = {"fatal": 0, "error": 1, "warning": 2, "information": 3}


def _generic_location(issue):
    """Location with the Bundle.entry[n].resource prefix swapped for the
    entry's resource type, so the same problem in many entries groups."""
    loc = issue.get("location") or ""
    if "entry" in issue and issue.get("entry_type"):
        return re.sub(r"^Bundle\.entry\[\d+\]\.resource(?:\.ofType\(\w+\))?", issue["entry_type"], loc)
    return loc


def _group_issues(issues):
    """Collapses identical issues (same severity, location once the entry
    index is removed, and message) into one row listing every place it
    occurs — so the same problem across many Observations is one line."""
    groups = {}
    for issue in issues:
        key = (issue["severity"], _generic_location(issue), issue.get("message") or "")
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"severity": issue["severity"], "location": key[1], "message": key[2],
                               "occurrences": [], "suggest": [], "mismatch_note": None}
        g["occurrences"].append({"entry_label": issue.get("entry_label"), "line": issue.get("line"),
                                 "location": issue.get("location")})
        for sug in issue.get("suggest") or []:
            if sug not in g["suggest"]:
                g["suggest"].append(sug)
        g["mismatch_note"] = g["mismatch_note"] or issue.get("mismatch_note")
    return sorted(groups.values(), key=lambda g: (_SEVERITY_ORDER.get(g["severity"], 9), g["location"]))


def build_report(jobs, include_information=False, ignore=None):
    """What both the page and the PDF show: per validated resource, only
    errors and warnings (plus information if asked), minus anything
    matching `ignore`. Returns {sections, totals, ignored_total,
    include_information, ignore}."""
    ignore = [p.strip() for p in (DEFAULT_IGNORED_ISSUES if ignore is None else ignore) if p.strip()]
    lowered = [p.lower() for p in ignore]
    wanted = {"fatal", "error", "warning"} | ({"information"} if include_information else set())
    sections, totals, ignored_total = [], {"error": 0, "warning": 0, "information": 0}, 0
    for job in jobs:
        issues, ignored = [], 0
        for issue in job.get("issues") or []:
            if issue["severity"] not in wanted:
                continue
            if any(p in (issue.get("message") or "").lower() for p in lowered):
                ignored += 1
                continue
            issues.append(issue)
        counts = {"error": 0, "warning": 0, "information": 0}
        for issue in issues:
            counts["error" if issue["severity"] == "fatal" else issue["severity"]] += 1
        groups = _group_issues(issues)
        for k in totals:
            totals[k] += counts[k]
        ignored_total += ignored
        sections.append({
            "key": job["key"], "label": job["label"], "profile": job.get("profile"),
            "ig_label": job.get("ig_label"), "bundle_rule_text": job.get("bundle_rule_text"),
            "individual": job.get("key") != "all", "error": job.get("error"),
            "stale": bool(job.get("stale")), "auto": bool(job.get("auto")),
            "represents": job.get("represents") or [],
            "resource_type": (job.get("resource") or {}).get("resourceType"),
            "issues": issues, "groups": groups, "counts": counts, "ignored": ignored,
            "has_entries": any("entry" in i for i in issues),
            "json": job.get("json"), "log": job.get("log"),
        })
    # "Unable to find a profile match" rows point at the section where the
    # referenced resource was validated on its own (the automatic follow-up).
    # An entry that wasn't validated because another of its type stands in
    # for it (FOLLOW_UP_FIRST_ONLY_TYPES) points at that one instead.
    index_by_key = {sec["key"]: (i, sec["label"]) for i, sec in enumerate(sections, 1)}
    index_by_label = {}
    for i, sec in enumerate(sections, 1):
        for label in sec.get("represents") or []:
            index_by_label.setdefault(label, (i, sec["label"]))
    for sec in sections:
        for g in sec["groups"]:
            see = []
            for sug in g["suggest"]:
                target = index_by_key.get(sug["key"]) or index_by_label.get(sug["label"])
                if target and sug["key"] != sec["key"] and target not in see:
                    see.append(target)
            g["see"] = see
    report = {"sections": sections, "totals": totals, "ignored_total": ignored_total,
              "include_information": include_information, "ignore": ignore}
    report.update(_overall_result(sections, totals))
    report["advice"] = _advice(sections, totals)
    return report


#: Overall result: any error fails; warnings alone pass with warnings; a
#: resource the validator couldn't check at all makes it incomplete.
RESULT_LABELS = {
    "fail": "Fail",
    "incomplete": "Incomplete",
    "pass_with_warnings": "Pass with warnings",
    "pass": "Pass",
}


def _overall_result(sections, totals):
    failed_runs = [s["label"] for s in sections if s.get("error")]
    if totals["error"]:
        result = "fail"
        reason = f"{totals['error']} validation error(s)"
        if failed_runs:
            reason += f"; {len(failed_runs)} resource(s) could not be validated"
    elif failed_runs:
        result = "incomplete"
        reason = (f"{len(failed_runs)} resource(s) could not be validated "
                  f"({', '.join(failed_runs)}) — no errors in the rest")
    elif totals["warning"]:
        result = "pass_with_warnings"
        reason = f"no errors, {totals['warning']} warning(s)"
    else:
        result = "pass"
        reason = "no errors or warnings"
    return {"result": result, "result_label": RESULT_LABELS[result], "result_reason": reason}


def _distinct(sections, severities):
    """(occurrences, distinct problems) of these severities — distinct
    being one per grouped row, ignoring which resource it was found in."""
    occurrences, distinct = 0, set()
    for s in sections:
        for g in s["groups"]:
            if g["severity"] in severities:
                occurrences += len(g["occurrences"])
                distinct.add((g["severity"], g["location"], g["message"]))
    return occurrences, len(distinct)


def _advice(sections, totals):
    """Plain-language next steps for the report, based on what it found:
    the same problem tends to repeat (many Observations built the same
    way; "Unable to find a profile match" restating a referenced
    resource's own errors), so the point is to find the few root causes."""
    advice = []
    errors, distinct_errors = _distinct(sections, {"fatal", "error"})
    mismatches = sum(len(g["occurrences"]) for s in sections for g in s["groups"]
                     if g["severity"] in ("fatal", "error") and _profile_mismatch(g))
    auto = [s for s in sections if s.get("auto")]
    if errors:
        advice.append("Fix errors first — they are what fails the report. Warnings don't, and can be "
                      "reviewed once the errors are gone.")
    if errors and distinct_errors < errors:
        advice.append(f"The {errors} error(s) come from {distinct_errors} distinct problem(s). Identical "
                      "issues are shown once, with every place they occur — each is usually one fault in "
                      "how the sending system builds that resource, so fixing it once fixes every "
                      "occurrence.")
    if mismatches:
        advice.append(f"{mismatches} error(s) are \"Unable to find a profile match\". These are symptoms: they "
                      "say a referenced resource doesn't conform to its profile, not why. "
                      + ("The reasons are in the automatically validated resource(s) below — fix those and "
                         "these errors should go away too." if auto else
                         "Validate the referenced resource on its own to see why."))
    for s in auto:
        if s.get("represents"):
            n = len(s["represents"])
            advice.append(f"Only the first {s['resource_type']} ({s['label']}) was validated on its own. "
                          f"{n} other {s['resource_type']}(s) were named in the same errors and are very likely "
                          f"built the same way, so expect them to have the same problems: apply the fixes for "
                          f"{s['label']} to all of them.")
    if any(s.get("error") for s in sections):
        advice.append("Some resources could not be validated at all (see their sections) — the report can't "
                      "pass until they are.")
    if errors:
        advice.append("Re-validate after each round of fixes: fixing one error can remove others or let "
                      "the validator check further and report new ones.")
    elif totals["warning"]:
        advice.append("Review the warnings: they don't fail the report but often point at data that a "
                      "receiving system may not handle as expected.")
    return advice
