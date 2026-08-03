# Deploying on Windows Server with IIS

This app runs as a standard WSGI application. On Windows Server, IIS doesn't
host WSGI apps directly — the pattern here is:

- **Waitress** (a pure-Python WSGI server) runs the Flask app as a
  background **Windows Service**, listening on `127.0.0.1:8000`.
- **IIS** terminates TLS and reverse-proxies all traffic to that service
  via the URL Rewrite + Application Request Routing (ARR) modules.

This keeps the app as a single long-lived process — important here, since
`FhirClient` caches things at class/process level (the reference-resolution
cache, resolved ICB boundary polygons, and the lazily-loaded scispaCy/UMLS
models) that would otherwise be discarded every time IIS recycled a worker
process under an in-process hosting model (e.g. `wfastcgi`).

## 1. Prerequisites on the server

- **Python 3.x** installed (matching whatever version you developed
  against), added to PATH or noted for use with a full path.
- **IIS** installed via Server Manager (Web Server (IIS) role).
- **URL Rewrite** module —
  https://www.iis.net/downloads/microsoft/url-rewrite
- **Application Request Routing (ARR)** —
  https://www.iis.net/downloads/microsoft/application-request-routing
- **NSSM** (Non-Sucking Service Manager), used to run the Python process as
  a Windows Service — https://nssm.cc/download
- Network access from this server to:
  - the FHIR base URL (`FHIR_BASE_URL`)
  - `postcodes.io` and the ONS ArcGIS FeatureServer, if using the `/stats`
    maps (server-side geocoding/boundary calls)
  - the internet, on first use of clinical-term extraction, to download the
    scispaCy model + UMLS knowledge base (~150MB + ~1GB) — see README

## 2. Copy the app and install dependencies

```powershell
# e.g. C:\apps\julius
cd C:\apps\julius

py -3 -m venv venv
venv\Scripts\pip install -r requirements.txt
```

`requirements.txt` already includes `waitress`. This step also pulls in
`scispacy`'s NER model (~150MB) — see the README note if you want to skip
that for now.

## 3. Set environment variables

Set these as **System** (not user) environment variables, since the
Windows Service will run under a service account, not your interactive
login: *System Properties → Advanced → Environment Variables → System
variables → New*.

| Variable          | Example                                              |
|--------------------|-------------------------------------------------------|
| `FHIR_BASE_URL`     | `https://192.168.1.62/healthconnect/cdr/fhir/r4`      |
| `FHIR_VERIFY_SSL`   | `true` (if the FHIR server has a real, non-self-signed cert) |
| `SECRET_KEY`        | a long random string (`python -c "import secrets; print(secrets.token_hex(32))"`) |
| `FLASK_DEBUG`       | `0` (disables the Werkzeug debugger — leave unset only for local dev) |
| `PORT`              | `8000` (optional — matches `wsgi.py`'s default)       |

There's no `FHIR_USER`/`FHIR_PASSWORD` to set any more — users authenticate
via the app's own `/login` screen with their own FHIR server credentials
(see CLAUDE.md's Authentication section). `SECRET_KEY` matters more in this
deployment model than it would otherwise: without a fixed value the app
generates a random one at each service start, which invalidates every
logged-in session (everyone has to sign in again) on every restart —
annoying on a service that NSSM might restart automatically after a crash.

A machine reboot (or at least a fresh service start) is needed after
setting these for the service to pick them up.

## 4. Install the app as a Windows Service (NSSM)

From an elevated PowerShell/cmd prompt:

```powershell
nssm install JuliusApp "C:\apps\julius\venv\Scripts\python.exe" "C:\apps\julius\wsgi.py"
nssm set JuliusApp AppDirectory "C:\apps\julius"
nssm set JuliusApp AppStdout "C:\apps\julius\logs\service.log"
nssm set JuliusApp AppStderr "C:\apps\julius\logs\service.log"
nssm set JuliusApp Start SERVICE_AUTO_START

nssm start JuliusApp
```

(Create the `logs` folder first: `mkdir C:\apps\julius\logs`.)

Confirm it's up before wiring IIS to it:

```powershell
curl http://127.0.0.1:8000/
```

You should get the app's HTML back. Check `logs\service.log` if not.

## 5. Configure IIS as a reverse proxy

1. **Enable ARR's proxy function** (server-wide, one-time): in IIS
   Manager, click the **server node** (top of the tree, not a site) →
   **Application Request Routing Cache** → **Server Proxy Settings...** →
   check **Enable proxy** → **Apply**.
2. **Create a site** (or use an existing one) in IIS Manager pointing at an
   empty physical path — it doesn't need to contain `app.py`, just the
   `web.config` below.
3. Copy `web.config` (already in the repo root) into that site's physical
   path. It contains the rewrite rule sending all traffic to
   `http://127.0.0.1:8000/{R:1}`. If you changed `PORT` in step 3, update
   the port in `web.config` to match.
4. Bind the site to your hostname/port, and attach your real TLS
   certificate to the HTTPS binding — IIS terminates TLS here; Waitress
   itself is only ever spoken to over plain HTTP on localhost.

## 6. Verify

- Browse to the site's HTTPS URL from another machine — you should see the
  app's patient search page.
- Try a search that hits the FHIR server, to confirm outbound connectivity
  works from the service account Waitress runs under (not just from your
  own interactive session).
- If using `/stats`'s maps, open it once and check the organisation map /
  ICS choropleth render — if either comes up empty, see the "Things worth
  double-checking against your server" section in the main README (postcode
  geocoding, ICB name matching, etc. are separate from this deployment
  setup and covered there).

## Troubleshooting

- **502/504 from IIS**: the Waitress service isn't running or isn't
  listening on the port `web.config` points at — check
  `nssm status JuliusApp` and `logs\service.log`.
- **IIS serves a directory listing or 403 instead of proxying**: the URL
  Rewrite rule isn't being applied — confirm both URL Rewrite and ARR are
  installed and that "Enable proxy" (step 5.1) was actually checked; it's
  a separate step from installing the modules.
- **App loads but every FHIR-backed page errors**: the service account
  Waitress runs under doesn't have the environment variables from step 3,
  or can't reach `FHIR_BASE_URL` over the network — Windows Services often
  run under `LocalSystem` or a dedicated service account with different
  network routing/firewall rules than your own login.
- **First clinical-terms extraction request hangs for a long time**: normal
  — it's downloading the ~1GB UMLS knowledge base on first use, cached
  under the service account's profile afterwards. Confirm that account's
  profile directory is writable and the server has outbound internet
  access.
