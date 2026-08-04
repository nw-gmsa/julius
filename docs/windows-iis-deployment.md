# Deploying on Windows Server with IIS

This app runs as a standard WSGI application. On Windows Server, IIS doesn't
host WSGI apps directly — the pattern here is:

- **Waitress** (a pure-Python WSGI server) runs the Flask app as a
  background **Windows Service**, listening on `127.0.0.1:8000`.
- **IIS** terminates TLS and reverse-proxies all traffic to that service
  via the URL Rewrite + Application Request Routing (ARR) modules.

This keeps the app as a single long-lived process — important here, since
`FhirClient` caches things at class/process level (the reference-resolution
cache and resolved ICB boundary polygons) that would otherwise be discarded
every time IIS recycled a worker process under an in-process hosting model
(e.g. `wfastcgi`).

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

## 2. Copy the app and install dependencies

```powershell
# e.g. C:\apps\julius
cd C:\apps\julius

py -3 -m venv venv
venv\Scripts\pip install -r requirements.txt
```

`requirements.txt` already includes `waitress`.

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
| `URL_PREFIX`        | `/julius` — only set this if deploying under a URL sub-path (see "Deploying under a URL sub-path" below); leave unset for a site bound at its own root |

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
.\nssm.exe install JuliusApp "E:\apps\julius\venv\Scripts\python.exe" "E:\apps\julius\wsgi.py"
.\nssm.exe set JuliusApp AppDirectory "E:\apps\julius"
.\nssm.exe set JuliusApp AppStdout "E:\apps\julius\logs\service.log"
.\nssm.exe set JuliusApp AppStderr "E:\apps\julius\logs\service.log"
.\nssm.exe set JuliusApp Start SERVICE_AUTO_START

.\nssm.exe start JuliusApp
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

## 5a. Deploying under a URL sub-path (IIS Application)

If instead of its own site/binding this app is deployed as an **IIS
Application** under an existing site — e.g. an Application named `julius`
so it's reachable at `https://host/julius/` rather than `https://host/` —
one extra step is needed, or every link the app renders will point back at
the site root instead of staying under `/julius`.

- `web.config`'s rewrite rule needs no change: IIS Application routing
  already evaluates URL Rewrite rules *relative to the Application's own
  root*, so a request for `https://host/julius/patient/123` reaches
  Waitress as plain `/patient/123` — routing works with zero app changes.
- What breaks without the next step: this app renders a lot of literal
  `href="/patient/..."`-style links (not exclusively `url_for()`), and
  Flask itself has no way to know it's mounted under `/julius` unless
  told. Every link/redirect/form action would come out pointing at the
  domain root, taking you straight out of the Application the moment you
  click anything.
- **Set the `URL_PREFIX` System environment variable** (see the table in
  step 3) to the Application's path, e.g. `/julius`, then restart the
  `JuliusApp` service. `wsgi.py`'s `PrefixMiddleware` reads it and sets
  the WSGI `SCRIPT_NAME` on every request; Flask's `url_for()` picks that
  up automatically (via `request.script_root`), and the templates that use
  literal paths read the same value as `{{ request.script_root }}` to
  prepend it themselves.
- Leave `URL_PREFIX` unset (the default) for a root-bound site — that's
  the common case covered by steps 1-5 above, and needs no middleware
  involvement at all.

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
- **Deployed under a sub-path (e.g. `/julius`) and links/redirects keep
  landing you back at the domain root**: `URL_PREFIX` isn't set (or the
  service wasn't restarted after setting it) — see "Deploying under a URL
  sub-path" above. Confirm with `curl -I https://host/julius/` that the
  login page's `Set-Cookie`/links reference `/julius/...`, not `/...`.
