# Registering this app against Epic's FHIR sandbox

`epic_client.py`'s `EpicClient` authenticates via SMART Backend Services
(OAuth2 JWT-bearer client-credentials) — there's no username/password and
no interactive login, just a JWT this app signs with a private key and
Epic verifies against a public key it fetches itself. Getting
`EPIC_CLIENT_ID`/`EPIC_SCOPE` set requires registering this app on Epic's
own developer site; this isn't something that can be automated from this
repo. See `CLAUDE.md`'s "Epic FHIR connectivity" section for how the
resulting config is used in code.

This walks through getting a **Non-Production Client ID** against Epic's
public sandbox — the initial target before a real Manchester Foundation
Trust (MFT) test environment exists (see `EPIC_FHIR_BASE_URL_DEFAULT` in
`epic_client.py`).

## 1. Get an Epic developer account

Sign in / register for free at [open.epic.com](https://open.epic.com) —
no purchase or approval needed for sandbox access.

## 2. Generate this app's key pair and JWKS

Already done once for this repo (`kid=epic-sandbox-2026-09`,
`epic/jwks.json`) — see `scripts/generate_epic_jwks.py` if a fresh key
pair is ever needed (e.g. key rotation, or a lost private key):

```bash
python3 scripts/generate_epic_jwks.py --kid <a-new-kid>
```

This writes the **private** key to a local, git-ignored PEM file (never
commit it) and adds the **public** half to `epic/jwks.json` (which *is*
committed — see step 4 for why).

## 3. Register the app

Go to [fhir.epic.com/Developer/Apps](https://fhir.epic.com/Developer/Apps)
→ "Create App". Pick **"Backend Systems"** as the application type — not
"Patient Facing" or "Provider Facing", which are interactive SMART-launch
types this app doesn't use.

## 4. Pick scopes

Select the FHIR resources/operations this app needs — for what's built so
far in `epic_client.py`, that's roughly:

- `Patient.Read`
- `DiagnosticReport.Read`
- `Observation.Read`
- `FamilyMemberHistory.Read`

Whatever's selected here becomes the `EPIC_SCOPE` value below
(space-separated `system/Resource.Read` tokens).

## 5. Supply the public key — via JWKS URL

Epic asks how it should verify this app's signed JWTs: either upload a
single public key, or give it a **JWKS URL** it fetches itself. Choose
JWKS URL and point it at this repo's committed `epic/jwks.json`, hosted
from GitHub rather than served by the Flask app:

```
https://raw.githubusercontent.com/nw-gmsa/julius/main/epic/jwks.json
```

**Before registering this URL**: `epic/jwks.json` needs to actually be
committed and pushed, and the repo (or at least this file, e.g. via
GitHub Pages) needs to be publicly fetchable — Epic's authorization
server reaches this URL over the open internet to verify a token
request. Not yet confirmed whether `nw-gmsa/julius` is public.

## 6. Submit

Epic issues a **Non-Production Client ID** immediately (no review wait) —
that's `EPIC_CLIENT_ID` below. A separate Production Client ID is also
generated but stays inert until a real health system (eventually MFT)
authorizes this app for go-live.

## 7. Wire it into `.env`

```bash
EPIC_CLIENT_ID=<the Non-Production Client ID Epic gives you>
EPIC_JWT_KID=epic-sandbox-2026-09   # already set — matches epic/jwks.json's kid
EPIC_SCOPE=<whatever was selected in step 4, space-separated>
```

`EPIC_PRIVATE_KEY_PATH` is already set (in `.env`) to the key file
generated in step 2. `EPIC_FHIR_BASE_URL` doesn't need setting for the
sandbox — it defaults to Epic's public non-production endpoint.

## 8. Test it

Restart the app, sign in, go to **Epic → Test connection** in the nav
(`/epic?test=1`). This calls `EpicClient.verify_connection()` — fetches
the server's `CapabilityStatement`, then acquires an access token using
the config above.
