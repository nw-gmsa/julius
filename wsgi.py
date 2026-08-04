"""Production entry point — runs the Flask app under Waitress instead of the
Werkzeug dev server started by `python app.py`.

Intended to be run as a Windows Service (e.g. via NSSM) behind an IIS
reverse proxy. See README/CLAUDE.md for the full IIS deployment steps.
"""
import os

from waitress import serve

from app import app


class PrefixMiddleware:
    """
    Makes the app aware it's reverse-proxied under a URL prefix (e.g.
    IIS Application "julius" bound at https://host/julius/) rather than at
    the domain root — set via the URL_PREFIX env var (e.g. "/julius"),
    empty/unset for a root deployment (the default, unchanged behaviour).

    web.config's URL Rewrite rule already forwards paths *relative to the
    IIS Application* (i.e. already stripped of the prefix — an IIS
    Application's rewrite rules only ever see its own sub-tree), so
    PATH_INFO reaching Waitress is already correct and untouched here.
    What's missing without this middleware is SCRIPT_NAME: Flask's own
    url_for()/redirect(url_for(...)) calls derive the prefix from
    `request.script_root`, which Werkzeug populates straight from
    environ['SCRIPT_NAME'] — so setting that one WSGI environ key here is
    enough to make every url_for()-based link/redirect in the app come out
    correctly prefixed.

    This does *not* fix hardcoded absolute template links (this codebase
    has plenty of literal href="/patient/..." style paths, not url_for())
    — those templates prepend {{ request.script_root }} themselves, which
    reads the same environ value this middleware sets.
    """

    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        if self.prefix:
            environ["SCRIPT_NAME"] = self.prefix
        return self.wsgi_app(environ, start_response)


application = PrefixMiddleware(app, os.environ.get("URL_PREFIX", ""))

if __name__ == "__main__":
    host = os.environ.get("WSGI_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 8000))
    serve(application, host=host, port=port)
