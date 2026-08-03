"""Production entry point — runs the Flask app under Waitress instead of the
Werkzeug dev server started by `python app.py`.

Intended to be run as a Windows Service (e.g. via NSSM) behind an IIS
reverse proxy. See README/CLAUDE.md for the full IIS deployment steps.
"""
import os

from waitress import serve

from app import app

if __name__ == "__main__":
    host = os.environ.get("WSGI_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 8000))
    serve(app, host=host, port=port)
