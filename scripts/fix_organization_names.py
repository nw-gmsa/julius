#!/usr/bin/env python3
"""
Finds every Organization resource on the configured FHIR server
(FhirClient.organizations_without_name()) that has no `.name`, and
backfills one from the NHS ODS lookup API
(https://directory.spineservices.nhs.uk/ORD/2-0-0 — open access, no key
required) using the Organization's own ODS code identifier
(FhirClient.organisation_ods_code()).

An Organization with no ODS code identifier can't be corrected this way
and is reported separately, same for one whose ODS code the lookup API
doesn't recognise.

Dry-run by default — prints what it would change without touching the
server. Pass --apply to actually PUT the corrected name back.

Usage:
    python3 scripts/fix_organization_names.py            # dry run
    python3 scripts/fix_organization_names.py --apply     # writes changes
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fhir_client import FhirClient  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write the corrected name to the FHIR server (default: dry run only).",
    )
    args = parser.parse_args()

    try:
        client = FhirClient()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    orgs = client.organizations_without_name()

    if not orgs:
        print("No Organization resources without a name found.")
        return

    fixed, no_ods, lookup_failed = [], [], []
    for org in orgs:
        org_id = org.get("id")
        ods_code = FhirClient.organisation_ods_code(org)
        if not ods_code:
            no_ods.append(org_id)
            continue

        name = FhirClient.ods_lookup_name(ods_code)
        if not name:
            lookup_failed.append((org_id, ods_code))
            continue

        if args.apply:
            client.update_organization_name(org, name)
            print(f"APPLIED  - Organization/{org_id}: set name to {name!r} (ODS {ods_code})")
        else:
            print(f"DRY RUN  - Organization/{org_id}: would set name to {name!r} (ODS {ods_code})")
        fixed.append(org_id)

    print()
    print(f"{len(orgs)} organization(s) without a name found.")
    print(f"{len(fixed)} {'updated' if args.apply else 'would be updated'} from an ODS lookup.")
    if no_ods:
        print(f"{len(no_ods)} have no ODS code identifier to look up: {', '.join(no_ods)}")
    if lookup_failed:
        details = ", ".join(f"{oid} (ODS {code})" for oid, code in lookup_failed)
        print(f"{len(lookup_failed)} have an ODS code the lookup API didn't resolve: {details}")

    if not args.apply and fixed:
        print()
        print("Re-run with --apply to write these changes to the FHIR server.")


if __name__ == "__main__":
    main()
