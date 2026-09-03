"""
Connectivity to an Epic FHIR R4 endpoint via SMART Backend Services —
see https://fhir.epic.com/Documentation?docId=oauth2&section=BackendOAuth2Guide

This is a separate integration from fhir_client.py's FhirClient: that one
authenticates per-user via HTTP Basic against the NW GMSA server this app
is otherwise built around (see CLAUDE.md's Authentication section);
EpicClient instead authenticates as a registered *backend application* —
no user session, no username/password — against an Epic instance.
**Deliberately decoupled from the NW GMSA side of this app during
development** — nothing here cross-references a NW GMSA Patient/order/
report id, and nothing in fhir_client.py/app.py's existing routes calls
into this module. The plan is to start linking the two once there's a
real MFT (Manchester Foundation Trust) test environment to develop
against; until then this stays a standalone module.

The initial target is Epic's own public non-production sandbox —
EPIC_FHIR_BASE_URL_DEFAULT below
(https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4, per
https://fhir.epic.com/Documentation?docId=testingguide) — not MFT's own
instance, which doesn't exist yet for this project. Still fully
overridable via EPIC_FHIR_BASE_URL once a real MFT non-production
endpoint is available.

Connectivity plus a first cut of genomics/family-history reads are
implemented here: config validation, token acquisition, a generic
authenticated GET, verify_connection(), diagnostic_reports_for_patient()/
observations_for_report() (genomic reports), and
family_history_for_patient()/group_family_history() (family
history/pedigree — see "Family history / pedigree" below). Nothing here
is wired into app.py routes yet — there's no registered sandbox app or
real test data to develop a UI against until that exists.

SMART Backend Services, in short: this app is registered on
fhir.epic.com as a "backend system" with an RSA public key (or JWKS URL)
uploaded ahead of time. To get an access token, this app signs a JWT
with the matching *private* key (RS384, claims below) and trades it for
a bearer token via OAuth2's JWT-bearer client-credentials grant — no
client secret, no user login, ever. This needs a JWT library capable of
RS384 signing (PyJWT + cryptography — pip install "pyjwt[crypto]"; see
requirements.txt) since Python's standard library has no RSA signing of
its own.

**The JWKS Epic verifies these JWTs against is hosted from this GitHub
project, not served by the Flask app itself** — see
scripts/generate_epic_jwks.py, which generates an RSA key pair, writes
the private key to a local git-ignored PEM file, and commits the public
half as a JWK into epic/jwks.json. That file's GitHub raw URL
(https://raw.githubusercontent.com/nw-gmsa/julius/main/epic/jwks.json)
is what gets registered as this app's JWKS URL on fhir.epic.com — Epic's
authorization server fetches it directly from GitHub to verify a JWT's
signature, matching by the "kid" header
(EPIC_JWT_KID) against the JWKS entry's own "kid". This requires the
repo (or at least that file, e.g. via GitHub Pages) to be publicly
fetchable — not verified from here which this repo currently is.
"""
import base64
import os
import time
import uuid
import requests
import jwt
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

#: How long a signed client-assertion JWT is valid for, in seconds.
#: Epic requires this to be short-lived (documented max: 5 minutes) —
#: kept comfortably under that.
CLIENT_ASSERTION_LIFETIME_SECONDS = 240

#: Standard SMART discovery document path, relative to the FHIR base —
#: used to find the token endpoint when EPIC_TOKEN_URL isn't set
#: explicitly. Per https://build.fhir.org/ig/HL7/smart-app-launch/conformance.html
SMART_CONFIGURATION_PATH = "/.well-known/smart-configuration"

#: Epic's own public non-production FHIR sandbox — a real, stable,
#: publicly documented URL (not a secret, same reasoning as
#: FhirClient.ESB_TOKEN_URL_DEFAULT having a hardcoded default), used as
#: EPIC_FHIR_BASE_URL's default until a real MFT test endpoint replaces
#: it. Confirmed directly (not guessed) — see
#: https://fhir.epic.com/Documentation?docId=testingguide.
EPIC_FHIR_BASE_URL_DEFAULT = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"

#: DiagnosticReport.category token for genetics/genomics reports, per
#: HL7 v2-0074 — the same code fhir_client.py's DIAGNOSTIC_REPORT_CATEGORY
#: uses for the NW GMSA server. This is a real HL7-standard code, but
#: *not* confirmed against Epic's sandbox specifically (Epic's public
#: sandbox has no genomics test data to check it against — see
#: diagnostic_reports_for_patient()); diagnostic_reports_for_patient()
#: falls back to an unfiltered search if the categorized one returns
#: nothing, same try-then-fall-back pattern fhir_client.py's category
#: searches already use.
DIAGNOSTIC_REPORT_GENETICS_CATEGORY = "http://terminology.hl7.org/CodeSystem/v2-0074|GE"

#: Named patients on Epic's public non-production sandbox, confirmed
#: directly (FHIR id verified via a real Patient/<id> read, MRN verified
#: via a real Patient?identifier=<mrn> search returning the same id —
#: see docs/epic-sandbox-test-patients.md for the full writeup) rather
#: than guessed from Epic's own docs, which name several of these
#: patients but don't publish their ids/MRNs anywhere scrapable. Backing
#: data for the Pathology Explorer's "known test patients" quick-select
#: (see /pathology in app.py) — name search doesn't reliably find these
#: for a backend/system-level client (see search_patients()'s docstring),
#: so a hardcoded list of known-good ids/MRNs is what actually makes the
#: sandbox usable here, not a live directory lookup. `has_diagnostic_reports`
#: is only True for the two patients confirmed (end-to-end, via
#: /pathology) to actually have DiagnosticReport data — the other five
#: resolve fine as a patient but the Pathology Explorer's report list
#: comes back empty for them.
KNOWN_SANDBOX_TEST_PATIENTS = [
    {"name": "Camila Lopez", "fhir_id": "erXuFYUfucBZaryVksYEcMg3",
     "external_id": "Z6129", "mrn": "203713", "has_diagnostic_reports": True},
    {"name": "Derrick Lin", "fhir_id": "eq081-VQEgP8drUUqCWzHfw3",
     "external_id": "Z6127", "mrn": "203711", "has_diagnostic_reports": False},
    {"name": "Desiree Powell", "fhir_id": "eAB3mDIBBcyUKviyzrxsnAw3",
     "external_id": "Z6130", "mrn": "203714", "has_diagnostic_reports": False},
    {"name": "Elijah Davis", "fhir_id": "egqBHVfQlt4Bw3XGXoxVxHg3",
     "external_id": "Z6125", "mrn": "203709", "has_diagnostic_reports": False},
    {"name": "Linda Ross", "fhir_id": "eIXesllypH3M9tAA5WdJftQ3",
     "external_id": "Z6128", "mrn": "203712", "has_diagnostic_reports": False},
    {"name": "Olivia Roberts", "fhir_id": "eh2xYHuzl9nkSFVvV3osUHg3",
     "external_id": "Z6131", "mrn": "203715", "has_diagnostic_reports": False},
    {"name": "Warren McGinnis", "fhir_id": "e0w0LEDCYtfckT6N.CkJKCw3",
     "external_id": "Z6126", "mrn": "203710", "has_diagnostic_reports": True},
]

#: The HL7 v3-RoleCode system FamilyMemberHistory.relationship is bound
#: to by FHIR R4's own standard (extensible) binding — confirmed per
#: spec, not confirmed against Epic's sandbox specifically (see
#: FAMILY_RELATIONSHIP_INFO below).
FAMILY_RELATIONSHIP_CODESYSTEM = "http://terminology.hl7.org/CodeSystem/v3-RoleCode"

#: code -> (generation, side) for every code in HL7's "FamilyMember"
#: value set (http://terminology.hl7.org/ValueSet/v3-FamilyMember,
#: confirmed directly against terminology.hl7.org's own expansion, not
#: guessed) — what group_family_history() buckets a relative into
#: relative to the patient (generation 0):
#:   generation > 0 -> an ancestor's generation (1 = parent, 2 =
#:     grandparent, 3 = great-grandparent); < 0 -> a descendant's
#:     generation (-1 = child/niece/nephew, -2 = grandchild); 0 = same
#:     generation as the patient (siblings, cousins, spouse, in-laws).
#:   side -> "maternal"/"paternal" where the code itself encodes which
#:     side of the family (an M.../P... prefix, or "father"/"mother"
#:     lineage), else None.
#: A code not in this table (present but outside the FamilyMember value
#: set, or a system this app doesn't recognise) still gets shown by
#: relationship_info() — just with generation=side=None, in
#: group_family_history()'s "other" bucket rather than raising.
FAMILY_RELATIONSHIP_INFO = {
    "FAMMEMB": (None, None), "EXT": (None, None), "INLAW": (0, None),
    # Children / descendants (generation -1)
    "CHILD": (-1, None), "CHLDADOPT": (-1, None), "DAUADOPT": (-1, None), "SONADOPT": (-1, None),
    "CHLDFOST": (-1, None), "DAUFOST": (-1, None), "SONFOST": (-1, None), "DAUC": (-1, None),
    "DAU": (-1, None), "STPDAU": (-1, None), "NCHILD": (-1, None), "SON": (-1, None),
    "SONC": (-1, None), "STPSON": (-1, None), "STPCHLD": (-1, None),
    "CHLDINLAW": (-1, None), "DAUINLAW": (-1, None), "SONINLAW": (-1, None),
    "NIENEPH": (-1, None), "NEPHEW": (-1, None), "NIECE": (-1, None),
    # Grandchildren (generation -2)
    "GRNDCHILD": (-2, None), "GRNDDAU": (-2, None), "GRNDSON": (-2, None),
    # Same generation as the patient (0)
    "SIB": (0, None), "BRO": (0, None), "HBRO": (0, None), "NBRO": (0, None),
    "TWINBRO": (0, None), "FTWINBRO": (0, None), "ITWINBRO": (0, None), "STPBRO": (0, None),
    "HSIB": (0, None), "HSIS": (0, None), "NSIB": (0, None), "NSIS": (0, None),
    "TWINSIS": (0, None), "FTWINSIS": (0, None), "ITWINSIS": (0, None), "TWIN": (0, None),
    "FTWIN": (0, None), "ITWIN": (0, None), "SIS": (0, None), "STPSIS": (0, None), "STPSIB": (0, None),
    "SIBINLAW": (0, None), "BROINLAW": (0, None), "SISINLAW": (0, None), "SISLINLAW": (0, None),
    "SIGOTHR": (0, None), "DOMPART": (0, None), "FMRSPS": (0, None),
    "SPS": (0, None), "HUSB": (0, None), "WIFE": (0, None),
    "COUSN": (0, None), "MCOUSN": (0, "maternal"), "PCOUSN": (0, "paternal"),
    # Parents / aunts & uncles (generation 1)
    "PRN": (1, None), "ADOPTP": (1, None), "ADOPTF": (1, "paternal"), "ADOPTM": (1, "maternal"),
    "FTH": (1, "paternal"), "FTHFOST": (1, "paternal"), "NFTH": (1, "paternal"),
    "NFTHF": (1, "paternal"), "STPFTH": (1, "paternal"),
    "MTH": (1, "maternal"), "GESTM": (1, "maternal"), "MTHFOST": (1, "maternal"),
    "NMTH": (1, "maternal"), "NMTHF": (1, "maternal"), "STPMTH": (1, "maternal"),
    "NPRN": (1, None), "PRNFOST": (1, None), "STPPRN": (1, None),
    "PRNINLAW": (1, None), "FTHINLAW": (1, None), "MTHINLAW": (1, None), "MTHINLOAW": (1, None),
    "AUNT": (1, None), "MAUNT": (1, "maternal"), "PAUNT": (1, "paternal"),
    "UNCLE": (1, None), "MUNCLE": (1, "maternal"), "PUNCLE": (1, "paternal"),
    # Grandparents (generation 2)
    "GRPRN": (2, None), "GRFTH": (2, None), "MGRFTH": (2, "maternal"), "PGRFTH": (2, "paternal"),
    "GRMTH": (2, None), "MGRMTH": (2, "maternal"), "PGRMTH": (2, "paternal"),
    "MGRPRN": (2, "maternal"), "PGRPRN": (2, "paternal"),
    # Great-grandparents (generation 3)
    "GGRPRN": (3, None), "GGRFTH": (3, None), "MGGRFTH": (3, "maternal"), "PGGRFTH": (3, "paternal"),
    "GGRMTH": (3, None), "MGGRMTH": (3, "maternal"), "PGGRMTH": (3, "paternal"),
    "MGGRPRN": (3, "maternal"), "PGGRPRN": (3, "paternal"),
}


class EpicClient:
    """Backend-services client for an Epic FHIR R4 endpoint.

    Config is read fresh from the environment on every call
    (EpicClient.config(), same pattern as FhirClient.esb_config() in
    fhir_client.py) rather than cached at import time or on an instance,
    so changing an env var and restarting picks it up with no other code
    change. Every value below is read from an environment variable;
    EPIC_FHIR_BASE_URL is the one exception with a hardcoded default
    (Epic's own public sandbox — see EPIC_FHIR_BASE_URL_DEFAULT above),
    since unlike a real MFT endpoint it's a stable, publicly documented
    URL, not a secret or deployment-specific value:

    - EPIC_FHIR_BASE_URL    — the Epic FHIR R4 base. Defaults to Epic's
      public non-production sandbox (EPIC_FHIR_BASE_URL_DEFAULT);
      override once a real MFT test endpoint exists.
    - EPIC_CLIENT_ID        — this app's registered backend-system client
      id (Epic's "Non-Production Client ID" for a test/sandbox
      registration). Required, no default.
    - EPIC_PRIVATE_KEY_PATH — path to the PEM-encoded RSA private key
      matching the public key/JWKS uploaded for this app on
      fhir.epic.com — see scripts/generate_epic_jwks.py, which generates
      this key pair and writes the public half into the JWKS this repo
      hosts (epic/jwks.json). Required unless EPIC_PRIVATE_KEY is set
      instead.
    - EPIC_PRIVATE_KEY      — the PEM-encoded private key's contents
      directly (as opposed to a path) — an alternative for deployments
      where dropping a key file on disk is awkward (e.g. the IIS/Windows
      deployment in docs/windows-iis-deployment.md, which already sets
      other secrets via env vars in web.config). EPIC_PRIVATE_KEY_PATH
      wins if both are set.
    - EPIC_JWT_KID          — the "kid" (key id) JWT header value
      identifying which key in the hosted JWKS
      (scripts/generate_epic_jwks.py's --kid) this app is currently
      signing with — needed since that JWKS can hold more than one key
      (key rotation).
    - EPIC_SCOPE            — space-separated SMART scopes to request
      (e.g. "system/Patient.read system/Observation.read"), matching
      whatever this app was actually registered for. Required — unlike
      fhir_client.py's optional ESB_SCOPE, Epic's backend-services flow
      always needs an explicit scope.
    - EPIC_TOKEN_URL        — the OAuth2 token endpoint. Optional; if
      unset, discovered from EPIC_FHIR_BASE_URL + "/.well-known/
      smart-configuration" (SMART's standard discovery document) on
      first use and cached at class level.
    - EPIC_VERIFY_SSL       — TLS verification, default true (unlike
      FHIR_VERIFY_SSL's default-off, which exists specifically because
      the internal NW GMSA deployment uses a self-signed cert — a real
      Epic endpoint is expected to have a valid one).
    """

    #: Class-level OAuth2 token cache — not per-request, since a
    #: client-credentials token is valid for every request until it
    #: expires, regardless of who's using this app. Same shape as
    #: FhirClient._esb_token_cache.
    _token_cache = {"access_token": None, "expires_at": 0}

    #: Class-level cache of discovered token endpoints, keyed by FHIR
    #: base URL — same "static reference data, not worth re-fetching
    #: per request" reasoning as FhirClient._genomic_test_directory_cache.
    _discovered_token_url_cache = {}

    @classmethod
    def config(cls):
        """(base_url, client_id, private_key_pem, kid, scope, verify_ssl)
        — read fresh from the environment. Raises RuntimeError, naming
        exactly which variable(s) are missing, if any required value
        isn't set — same "fail clearly, don't guess" stance
        FhirClient.__init__ and IrisClient.__init__ already take."""
        base_url = (os.environ.get("EPIC_FHIR_BASE_URL") or EPIC_FHIR_BASE_URL_DEFAULT).rstrip("/")
        client_id = os.environ.get("EPIC_CLIENT_ID")
        key_path = os.environ.get("EPIC_PRIVATE_KEY_PATH")
        key_inline = os.environ.get("EPIC_PRIVATE_KEY")
        scope = os.environ.get("EPIC_SCOPE")
        kid = os.environ.get("EPIC_JWT_KID")
        verify_ssl = os.environ.get("EPIC_VERIFY_SSL", "true").lower() == "true"

        missing = []
        if not client_id:
            missing.append("EPIC_CLIENT_ID")
        if not key_path and not key_inline:
            missing.append("EPIC_PRIVATE_KEY_PATH (or EPIC_PRIVATE_KEY)")
        if not scope:
            missing.append("EPIC_SCOPE")
        if missing:
            raise RuntimeError(
                "EpicClient is not configured — missing environment variable(s): "
                + ", ".join(missing)
            )

        if key_path:
            with open(key_path, "r", encoding="utf-8") as f:
                private_key_pem = f.read()
        else:
            private_key_pem = key_inline

        return base_url, client_id, private_key_pem, kid, scope, verify_ssl

    @classmethod
    def _token_url(cls, base_url, verify_ssl):
        """EPIC_TOKEN_URL if set, otherwise the token endpoint from
        base_url's SMART discovery document, cached per base_url for the
        process lifetime (a failed discovery fetch is not cached, so the
        next call retries — same convention
        FhirClient.fetch_icb_boundaries() uses for its own class-level
        cache)."""
        explicit = os.environ.get("EPIC_TOKEN_URL")
        if explicit:
            return explicit
        if base_url in cls._discovered_token_url_cache:
            return cls._discovered_token_url_cache[base_url]
        discovery_url = base_url + SMART_CONFIGURATION_PATH
        resp = requests.get(discovery_url, timeout=15, verify=verify_ssl)
        resp.raise_for_status()
        token_url = resp.json().get("token_endpoint")
        if not token_url:
            raise RuntimeError(
                f"SMART discovery document at {discovery_url} has no 'token_endpoint' "
                "— set EPIC_TOKEN_URL explicitly instead."
            )
        cls._discovered_token_url_cache[base_url] = token_url
        return token_url

    @staticmethod
    def _build_client_assertion(client_id, token_url, private_key_pem, kid):
        """Signs the JWT client_assertion Epic's backend-services token
        request needs — claims/lifetime per Epic's own guide (iss=sub=
        client_id, aud=token endpoint, a fresh jti per request, exp a few
        minutes out). Raises jwt.InvalidKeyError if private_key_pem isn't
        a valid PEM-encoded RSA private key."""
        now = int(time.time())
        claims = {
            "iss": client_id,
            "sub": client_id,
            "aud": token_url,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "nbf": now,
            "exp": now + CLIENT_ASSERTION_LIFETIME_SECONDS,
        }
        headers = {"kid": kid} if kid else None
        return jwt.encode(claims, private_key_pem, algorithm="RS384", headers=headers)

    @classmethod
    def access_token(cls, force_refresh=False):
        """OAuth2 bearer token via the JWT-bearer client-credentials
        grant (RFC 7523 / Epic's Backend Services flow) — no client
        secret, ever; authentication is the signed JWT itself. Cached at
        class level until 60s before it expires, same pattern as
        FhirClient.esb_access_token(); force_refresh=True bypasses the
        cache.

        Raises RuntimeError if required config is missing (see
        config()). Raises requests.HTTPError, with the authorization
        server's own error/error_description body folded into the
        message (RFC 6749 §5.2 — a plain raise_for_status() only gives a
        generic "400 Client Error"), if the token request is rejected —
        same "surface it, don't swallow it" approach as everywhere else
        in this app.
        """
        base_url, client_id, private_key_pem, kid, scope, verify_ssl = cls.config()
        cached = cls._token_cache
        if not force_refresh and cached["access_token"] and time.time() < cached["expires_at"]:
            return cached["access_token"]

        token_url = cls._token_url(base_url, verify_ssl)
        assertion = cls._build_client_assertion(client_id, token_url, private_key_pem, kid)
        data = {
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
            "scope": scope,
        }
        resp = requests.post(token_url, data=data, timeout=15, verify=verify_ssl)
        if not resp.ok:
            detail = None
            try:
                body = resp.json()
                detail = body.get("error_description") or body.get("error")
            except ValueError:
                detail = (resp.text or "").strip()[:500] or None
            message = f"Epic token request to {token_url} failed ({resp.status_code})"
            if detail:
                message += f": {detail}"
            raise requests.HTTPError(message, response=resp)

        token_data = resp.json()
        access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 300)
        cls._token_cache = {
            "access_token": access_token,
            "expires_at": time.time() + max(expires_in - 60, 0),
        }
        return access_token

    @classmethod
    def get(cls, path, params=None):
        """Authenticated GET against the Epic FHIR base — `path` is
        either a relative resource path (e.g. "Patient/abc123" or
        "Patient?_summary=count") or an absolute URL (e.g. a
        Bundle.link[rel=next] value, same convention as
        FhirClient._get()). One retry with a forced token refresh on a
        401, same reasoning as FhirClient.send_order_to_esb()'s retry.
        Returns the parsed FHIR JSON body; raises requests.HTTPError on
        any other failure."""
        base_url, _, _, _, _, verify_ssl = cls.config()
        url = path if path.startswith("http") else f"{base_url}/{path.lstrip('/')}"

        def _do_get(token):
            return requests.get(
                url, params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/fhir+json"},
                timeout=30, verify=verify_ssl,
            )

        resp = _do_get(cls.access_token())
        if resp.status_code == 401:
            resp = _do_get(cls.access_token(force_refresh=True))
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _entries(bundle, resource_type):
        """Bundle.entry[].resource for entries whose resourceType matches
        `resource_type` — every search method below goes through this
        rather than a bare `[e["resource"] for e in entries if
        "resource" in e]`. Confirmed directly against Epic's sandbox: a
        zero-match search doesn't just come back with an empty `entry`
        list, it bundles one entry wrapping an OperationOutcome
        ("Resource request returns no results.", warning severity) —
        the same style of quirk fhir_client.py has hit on its own server
        (see CLAUDE.md's ctDNA summary section, "not by trusting
        Bundle.entry.search.mode"), just via resource type instead. A
        naive filter treats that OperationOutcome as if it were a
        matched Patient/DiagnosticReport/etc — this was a real bug hit
        directly (search_patients() returning one result with a blank
        name/id for a search that actually matched nothing), not just a
        theoretical one; also means diagnostic_reports_for_patient()'s
        `not entries` fallback check couldn't previously trigger at all,
        since `entries` was never actually empty on a real zero-match
        response."""
        entries = bundle.get("entry") or []
        return [
            e["resource"] for e in entries
            if (e.get("resource") or {}).get("resourceType") == resource_type
        ]

    # ------------------------------------------------------------------
    # Patient search (Pathology Explorer)
    # ------------------------------------------------------------------

    @classmethod
    def get_patient(cls, patient_id):
        """Fetch a single Patient resource by id — the Pathology
        Explorer's counterpart to FhirClient.get_patient()."""
        return cls.get(f"Patient/{patient_id}")

    @classmethod
    def search_patients(cls, family=None, given=None, birthdate=None, identifier=None, patient_id=None):
        """Patient search against the Epic FHIR endpoint — the Pathology
        Explorer's counterpart to FhirClient.search_patients(). Epic's
        Patient.Search (R4) doesn't support a single free-text `name`
        param the way the NW GMSA server does, so this takes
        `family`/`given`/`birthdate` separately (a combination Epic's
        own testing guide documents for a demographic search) or
        `identifier` (e.g. an MRN) as an alternative, or `patient_id`
        for a direct lookup by id (mirroring
        FhirClient.search_patients()'s own patient_id shortcut — a 404
        is swallowed into `[]` rather than raised, same as that method).
        `identifier` takes priority if given alongside name/DOB fields,
        rather than combining both into one query. Refuses to run a
        completely unfiltered query (returns `[]` if nothing at all is
        given) — same not-worth-the-risk stance FhirClient.search_patients()/
        search_organizations() take against their own server (see
        CLAUDE.md's "413s on unfiltered system-wide searches"), even
        though Epic's sandbox hasn't shown the same 413 behaviour; there's
        no reason to ever fire a fully unscoped Patient search against a
        live FHIR server. Returns a plain list of resources
        (Bundle.entry[].resource), not a Bundle."""
        if patient_id:
            try:
                return [cls.get_patient(patient_id)]
            except requests.HTTPError:
                return []
        params = {}
        if identifier:
            params["identifier"] = identifier
        else:
            if family:
                params["family"] = family
            if given:
                params["given"] = given
            if birthdate:
                params["birthdate"] = birthdate
        if not params:
            return []
        bundle = cls.get("Patient", params=params)
        return cls._entries(bundle, "Patient")

    @classmethod
    def verify_connection(cls):
        """A minimal end-to-end check: fetches the server's
        CapabilityStatement (Epic's metadata endpoint doesn't require
        auth, so this alone confirms EPIC_FHIR_BASE_URL is reachable and
        really is a FHIR server) and then acquires an access token (which
        confirms EPIC_CLIENT_ID/the private key/EPIC_SCOPE are all
        actually accepted). Raises on either failure — same "surface it"
        stance as the rest of this module; nothing here swallows an
        error into a bool. Returns the CapabilityStatement's
        (fhirVersion, software.name) for a quick sanity check of what
        actually answered."""
        base_url, _, _, _, _, verify_ssl = cls.config()
        resp = requests.get(
            f"{base_url}/metadata",
            headers={"Accept": "application/fhir+json"},
            timeout=15, verify=verify_ssl,
        )
        resp.raise_for_status()
        capability = resp.json()

        cls.access_token()  # raises if auth itself is misconfigured/rejected

        fhir_version = capability.get("fhirVersion")
        software_name = (capability.get("software") or {}).get("name")
        return fhir_version, software_name

    # ------------------------------------------------------------------
    # Genomic reports (DiagnosticReport + Observation)
    # ------------------------------------------------------------------

    @classmethod
    def diagnostic_reports_for_patient(cls, patient_id, category=DIAGNOSTIC_REPORT_GENETICS_CATEGORY):
        """DiagnosticReport resources for `patient_id`, restricted to
        `category` (default DIAGNOSTIC_REPORT_GENETICS_CATEGORY — HL7
        v2-0074 "GE"). Falls back to an unfiltered patient search if the
        categorized one comes back empty, same try-then-fall-back
        pattern fhir_client.py's category searches already use — this
        category is a real HL7-standard code but not confirmed against
        Epic's sandbox (which has no genomics test data to check it
        against in the first place). Pass category=None to search
        unfiltered from the start. Returns a plain list of resources
        (Bundle.entry[].resource), not a Bundle."""
        params = {"patient": patient_id}
        if category:
            params["category"] = category
        bundle = cls.get("DiagnosticReport", params=params)
        reports = cls._entries(bundle, "DiagnosticReport")
        if not reports and category:
            bundle = cls.get("DiagnosticReport", params={"patient": patient_id})
            reports = cls._entries(bundle, "DiagnosticReport")
        return reports

    @classmethod
    def observations_for_report(cls, report):
        """Resolves a DiagnosticReport's result[] Observation references
        (each a relative reference like "Observation/abc123") via get().
        Skips (rather than raising on) any reference that fails to
        resolve, since one broken/inaccessible reference shouldn't blank
        out every other result — same "degrade gracefully per-item"
        stance FhirClient.resolve_specimens() takes."""
        observations = []
        for ref in report.get("result") or []:
            reference = ref.get("reference")
            if not reference:
                continue
            try:
                observations.append(cls.get(reference))
            except requests.HTTPError:
                continue
        return observations

    # ------------------------------------------------------------------
    # Orders and documents (ServiceRequest + DocumentReference)
    # ------------------------------------------------------------------

    @classmethod
    def service_requests_for_patient(cls, patient_id):
        """ServiceRequest resources for `patient_id` — orders, as
        opposed to diagnostic_reports_for_patient()'s results. A plain
        `ServiceRequest?patient=<id>` search, no category filter (unlike
        diagnostic_reports_for_patient()'s default) since there's no
        equivalent confirmed genetics-only category to narrow by here.
        `system/ServiceRequest.read` is granted for this app's client
        even though it isn't listed in EPIC_SCOPE in .env — confirmed
        directly from a real token response, so no scope change is
        needed for this to work. Returns a plain list of resources, not
        a Bundle."""
        bundle = cls.get("ServiceRequest", params={"patient": patient_id})
        return cls._entries(bundle, "ServiceRequest")

    @classmethod
    def document_references_for_patient(cls, patient_id):
        """DocumentReference resources for `patient_id` — clinical
        documents (e.g. scanned reports, letters), distinct from
        DiagnosticReport's structured results. A plain
        `DocumentReference?patient=<id>` search; same granted-scope note
        as service_requests_for_patient() above (`system/
        DocumentReference.read` is granted without being in EPIC_SCOPE).
        Returns a plain list of resources, not a Bundle. Note: a
        DocumentReference can carry its document's content inline as
        base64 in `content[].attachment.data` — potentially large — so
        callers pretty-printing the raw resource (e.g. the Pathology
        Explorer's "View FHIR" dialog) may show a sizeable blob for
        these; nothing here strips or truncates it."""
        bundle = cls.get("DocumentReference", params={"patient": patient_id})
        return cls._entries(bundle, "DocumentReference")

    @classmethod
    def conditions_for_patient(cls, patient_id):
        """Condition resources for `patient_id` — Epic's problem list,
        distinct from a FamilyMemberHistory entry's own `condition[]`
        (that's about a *relative's* condition, this is the patient's
        own). A plain `Condition?patient=<id>` search; `system/
        Condition.read` is granted for this app's client, same
        already-granted-without-being-in-EPIC_SCOPE situation as
        service_requests_for_patient()/document_references_for_patient()
        above. Verified directly against Camila Lopez: 2 real Condition
        resources, both category "Genomic Indicators"
        (`https://open.epic.com/FHIR/StructureDefinition/condition-category`
        code `"genomics"`) — pharmacogenomic metabolizer statuses
        (e.g. "CYP2B6 Intermediate Metabolizer") tied via `.evidence[]`
        back to the same Observation her Pharmacogenomic Panel
        DiagnosticReports already surface, not a separate/unrelated
        dataset. Returns a plain list of resources, not a Bundle."""
        bundle = cls.get("Condition", params={"patient": patient_id})
        return cls._entries(bundle, "Condition")

    @classmethod
    def get_document_reference(cls, document_id):
        """Fetch a single DocumentReference resource by id — a direct
        `Read`, not a `Search`, so (unlike search_patients()/
        diagnostic_reports_for_patient()) it doesn't need a patient id
        alongside it; used by the Pathology Explorer's "View document"
        link, which only has the DocumentReference id on the URL."""
        return cls.get(f"DocumentReference/{document_id}")

    @classmethod
    def fetch_attachment_bytes(cls, attachment):
        """Resolve a FHIR Attachment (e.g. one entry of a
        DocumentReference's `content[].attachment`) to raw bytes +
        content type — the Pathology Explorer's counterpart to
        `FhirClient.fetch_attachment_bytes()`, same two shapes: inlined
        base64 in `.data`, or a `.url` pointing at a FHIR **Binary**
        resource (e.g. "Binary/abc123"), requested as `Accept:
        application/fhir+json` (reliably returns a Binary resource — a
        JSON object with `contentType` and base64 `data`) and decoded,
        falling back to raw bytes if a server ignores the Accept header
        — same convention `FhirClient.fetch_attachment_bytes()` uses.
        One retry with a forced token refresh on a 401, same as `get()`.

        Note: at least one real Binary behind an Epic sandbox
        DocumentReference (an `application/pdf` attachment, on Camila
        Lopez's own document list) has been seen to fail server-side
        regardless of `Accept` header — a `400` from Epic itself
        ("Unknown error occurred formatting binary content."), confirmed
        not a request-format issue on this app's side since a sibling
        `text/html` attachment on the same patient resolves fine either
        way. Not swallowed here — raised like any other
        `requests.HTTPError` for the caller to surface, same
        "surface it, don't swallow it" stance as the rest of this app.
        """
        if attachment.get("data"):
            content_type = attachment.get("contentType", "application/octet-stream")
            return base64.b64decode(attachment["data"]), content_type

        url = attachment.get("url")
        if not url:
            return None, None
        base_url, _, _, _, _, verify_ssl = cls.config()
        full_url = url if url.startswith("http") else f"{base_url}/{url.lstrip('/')}"

        def _do_get(token):
            return requests.get(
                full_url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/fhir+json"},
                timeout=30, verify=verify_ssl,
            )

        resp = _do_get(cls.access_token())
        if resp.status_code == 401:
            resp = _do_get(cls.access_token(force_refresh=True))
        resp.raise_for_status()

        ctype_header = resp.headers.get("Content-Type", "")
        if "json" in ctype_header or "fhir" in ctype_header:
            binary_resource = resp.json()
            data_b64 = binary_resource.get("data")
            if not data_b64:
                return None, None
            content_type = binary_resource.get("contentType") or attachment.get("contentType", "application/octet-stream")
            return base64.b64decode(data_b64), content_type

        content_type = ctype_header or attachment.get("contentType", "application/octet-stream")
        return resp.content, content_type

    # ------------------------------------------------------------------
    # Family history / pedigree (FamilyMemberHistory)
    # ------------------------------------------------------------------
    #
    # Standard FamilyMemberHistory has no extension linking one entry to
    # another as its "parent" in a pedigree diagram — each entry just
    # describes one relative's history relative to the *patient*, via a
    # relationship code (see FAMILY_RELATIONSHIP_INFO above). Those codes
    # already encode the relative's position in the family tree (e.g.
    # "MGRFTH" = maternal grandfather), so a browsable family view can be
    # built directly from the flat per-patient list FamilyMemberHistory
    # search already returns — no separate pedigree resource/extension is
    # needed. group_family_history() below is that grouping.

    @classmethod
    def family_history_for_patient(cls, patient_id):
        """FamilyMemberHistory resources for `patient_id` — a plain,
        unfiltered `FamilyMemberHistory?patient=<id>` search (Epic
        confirms Search support for this resource in R4; no
        genomics-specific search parameter is documented for it, unlike
        Observation — see module docstring). Returns a plain list of
        resources, not a Bundle."""
        bundle = cls.get("FamilyMemberHistory", params={"patient": patient_id})
        return cls._entries(bundle, "FamilyMemberHistory")

    @staticmethod
    def relationship_info(family_member_history):
        """(label, generation, side) for one FamilyMemberHistory
        resource's `relationship` CodeableConcept — label is
        relationship.text if present, else the matched coding's own
        display, else the bare code; generation/side come from
        FAMILY_RELATIONSHIP_INFO, defaulting to (None, None) for a code
        that isn't in that table (an unrecognised code, or a
        relationship given as free text with no coding at all)."""
        relationship = family_member_history.get("relationship") or {}
        codings = relationship.get("coding") or []
        code = None
        display = None
        for coding in codings:
            if coding.get("system") == FAMILY_RELATIONSHIP_CODESYSTEM:
                code = coding.get("code")
                display = coding.get("display")
                break
        if code is None and codings:
            code = codings[0].get("code")
            display = codings[0].get("display")
        label = relationship.get("text") or display or code or "Unknown relationship"
        generation, side = FAMILY_RELATIONSHIP_INFO.get(code, (None, None))
        return label, generation, side

    @classmethod
    def group_family_history(cls, family_member_histories):
        """Groups a family_history_for_patient() list into a pedigree-
        style structure, keyed by generation relative to the patient
        (see FAMILY_RELATIONSHIP_INFO's docstring for the generation/side
        convention): {generation_or_"other": {side_or_"unspecified":
        [{"resource", "label"}, ...]}}. A relative whose relationship
        code isn't in FAMILY_RELATIONSHIP_INFO (generation is None) lands
        under the "other" key rather than being dropped, same
        don't-silently-lose-data stance as the rest of this app."""
        groups = {}
        for fmh in family_member_histories:
            label, generation, side = cls.relationship_info(fmh)
            gen_key = generation if generation is not None else "other"
            side_key = side or "unspecified"
            groups.setdefault(gen_key, {}).setdefault(side_key, []).append(
                {"resource": fmh, "label": label}
            )
        return groups
