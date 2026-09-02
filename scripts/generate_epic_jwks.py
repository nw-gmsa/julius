#!/usr/bin/env python3
"""
Generates an RSA key pair for epic_client.py's SMART Backend Services
authentication (see CLAUDE.md's "Epic FHIR connectivity" section) and
adds/updates its *public* half in a JSON Web Key Set (JWKS) file meant
to be committed to this repo and hosted from GitHub — Epic's app
registration needs a JWKS URL it can fetch to verify the JWTs this app
signs, and the plan is to host that JWKS from this GitHub project
itself (e.g. its raw.githubusercontent.com URL, or GitHub Pages) rather
than serving it from the Flask app.

The *private* half is written to a local, git-ignored PEM file (see
.gitignore's "*.pem"/"/epic_private_key*" entries) — it must never be
committed; only the public JWKS entry (kty/use/alg/kid/n/e — no private
material at all) goes into the repo.

Supports key rotation: re-running with a different --kid adds a second
entry to the JWKS's "keys" array (Epic can be told about a new key ahead
of switching this app over to it) rather than replacing the existing
one; pass --replace to instead drop any existing entry with the same
--kid before adding the new one (e.g. after a real key compromise).

Usage:
    python3 scripts/generate_epic_jwks.py --kid epic-2026-09
    python3 scripts/generate_epic_jwks.py --kid epic-2026-09 --replace
    python3 scripts/generate_epic_jwks.py --kid epic-2026-09 \\
        --key-out /secure/path/epic_private_key.pem --jwks-out epic/jwks.json
"""
import argparse
import base64
import json
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

DEFAULT_KEY_OUT = "epic_private_key.pem"
DEFAULT_JWKS_OUT = os.path.join("epic", "jwks.json")
DEFAULT_KEY_SIZE = 2048


def _b64url_uint(value):
    """Base64url-encodes an unsigned integer's big-endian bytes, no
    padding — the encoding RFC 7518 (JWA) §6.3.1 requires for a JWK's
    RSA "n"/"e" members."""
    length = (value.bit_length() + 7) // 8 or 1
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


def build_jwk(public_key, kid):
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS384",
        "kid": kid,
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }


def load_jwks(path):
    if not os.path.exists(path):
        return {"keys": []}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("keys", [])
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kid", required=True, help="Key id to publish this key under (e.g. 'epic-2026-09'). "
                                                       "Must match EPIC_JWT_KID for whichever deployment uses this key.")
    parser.add_argument("--key-out", default=DEFAULT_KEY_OUT,
                         help=f"Where to write the PRIVATE key PEM (default: {DEFAULT_KEY_OUT}, git-ignored). "
                              "Never commit this file.")
    parser.add_argument("--jwks-out", default=DEFAULT_JWKS_OUT,
                         help=f"Where to write/update the public JWKS JSON (default: {DEFAULT_JWKS_OUT}). "
                              "This one IS meant to be committed.")
    parser.add_argument("--key-size", type=int, default=DEFAULT_KEY_SIZE, help=f"RSA key size in bits (default: {DEFAULT_KEY_SIZE}).")
    parser.add_argument("--replace", action="store_true",
                         help="Drop any existing JWKS entry with this --kid before adding the new one, "
                              "instead of the default (append alongside it, for key-rotation overlap).")
    args = parser.parse_args()

    if os.path.exists(args.key_out):
        print(f"Error: {args.key_out} already exists — refusing to overwrite a private key. "
              "Pass a different --key-out if you intend to generate a new one.", file=sys.stderr)
        sys.exit(1)

    jwks = load_jwks(args.jwks_out)
    if any(k.get("kid") == args.kid for k in jwks["keys"]) and not args.replace:
        print(f"Error: {args.jwks_out} already has a key with kid '{args.kid}'. "
              "Pass --replace to replace it, or use a different --kid.", file=sys.stderr)
        sys.exit(1)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=args.key_size)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    os.makedirs(os.path.dirname(os.path.abspath(args.key_out)) or ".", exist_ok=True)
    with open(args.key_out, "wb") as f:
        f.write(private_pem)
    os.chmod(args.key_out, 0o600)

    jwks["keys"] = [k for k in jwks["keys"] if k.get("kid") != args.kid] + [build_jwk(private_key.public_key(), args.kid)]
    os.makedirs(os.path.dirname(os.path.abspath(args.jwks_out)) or ".", exist_ok=True)
    with open(args.jwks_out, "w", encoding="utf-8") as f:
        json.dump(jwks, f, indent=2)
        f.write("\n")

    print(f"Private key written to {args.key_out} (git-ignored — back this up securely; "
          "losing it means generating a new key and re-registering with Epic).")
    print(f"Public JWKS entry (kid='{args.kid}') written to {args.jwks_out} — commit this file.")
    print()
    print("Next steps:")
    print(f"  1. Commit and push {args.jwks_out} so it's reachable at its GitHub raw URL.")
    print("  2. Register that raw URL as this app's JWKS URL in Epic's backend-app configuration,")
    print(f"     along with kid '{args.kid}'.")
    print(f"  3. Set EPIC_PRIVATE_KEY_PATH={args.key_out} and EPIC_JWT_KID={args.kid} "
          "(plus EPIC_CLIENT_ID/EPIC_FHIR_BASE_URL/EPIC_SCOPE) wherever this app runs.")


if __name__ == "__main__":
    main()
