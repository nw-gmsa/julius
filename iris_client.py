"""
Read-only SQL access to RIE.PatientDemographics, a table in the
InterSystems IRIS database behind ENTERPRISESERVICEBUS — used only by the
/data-quality report (app.py). Deliberately separate from fhir_client.py:
this talks IRIS SQL over InterSystems' own Python driver (see the
iris_dbapi import fallback below for which module path that actually is
on the installed driver version), not FHIR REST, to a source-of-truth
demographics table that has no FHIR-side equivalent in this app (Patient
resources are downstream of it, not the same thing).

The report is built entirely around *why* a row's data-quality score is
low or null — see build_report()/_check_quality(). Most identifying
columns (NHS number, MRN, surname, forename, DOB, postcode, source,
score, last-updated) are located by best-effort name matching
(_find_column() against the *_PATTERNS lists below), since this table's
exact naming convention hasn't been confirmed against a real server. The
seven reason columns (NHSNumber plus the six PDS trace match/flag
columns) were given exact names, so those are located by
case-insensitive exact match instead (_find_exact_column()) — IRIS SQL
commonly upper-cases unquoted identifiers, so this still doesn't assume
one particular case.
"""
import os
import re

# intersystems_iris.dbapi._DBAPI (the module path this was originally
# built against) only exists on older driver releases. On the
# intersystems-irispython package currently on PyPI (5.x, pulled in via
# the intersystems-iris compatibility shim), `dbapi` is a plain
# *attribute* that package's __init__.py sets to the real driver module
# (iris.dbapi) — not a real importable submodule on disk. That means
# `import intersystems_iris.dbapi` (as a statement) raises
# ModuleNotFoundError even though `intersystems_iris.dbapi` (attribute
# access, after `import intersystems_iris`) works fine and has
# `.connect`. So: try the old submodule path first, then fall back to
# plain `import intersystems_iris` + `getattr(..., "dbapi", None)`
# rather than a second `import` statement — this was the actual cause
# of "isn't installed" showing up when the package was installed, just
# structured differently than assumed.
try:
    import intersystems_iris.dbapi._DBAPI as iris_dbapi
except ImportError:
    try:
        import intersystems_iris
    except ImportError:  # pragma: no cover - optional dependency, see requirements.txt
        iris_dbapi = None
    else:
        iris_dbapi = getattr(intersystems_iris, "dbapi", None)


#: Column-name patterns (case-insensitive regex) used to locate a
#: plausible identifying column without assuming this table's exact
#: naming convention — see module docstring.
NHS_NUMBER_PATTERNS = [r"nhs.?number", r"\bnhs.?no\b"]
DOB_PATTERNS = [r"date.?of.?birth", r"\bdob\b", r"birth.?date"]
POSTCODE_PATTERNS = [r"post.?code"]
SURNAME_PATTERNS = [r"surname", r"last.?name", r"family.?name"]
FORENAME_PATTERNS = [r"forename", r"first.?name", r"given.?name"]
#: Medical record number (hospital-assigned identifier, distinct from NHS
#: number — see MEDICAL_RECORD_NUMBER_TYPE in fhir_client.py for the
#: FHIR-side equivalent, though this table isn't FHIR).
MRN_PATTERNS = [r"\bmrn\b", r"medical.?record.?number", r"hospital.?number"]
#: The record's ODS-code source organisation, and its data-quality score.
SOURCE_PATTERNS = [r"\bsource\b", r"ods.?code", r"\bods\b"]
SCORE_PATTERNS = [r"\bscore\b", r"quality.?score", r"dq.?score"]
#: When the row was last updated — what the report's date range filters on.
LAST_UPDATED_PATTERNS = [r"last.?updated", r"last.?modified", r"updated.?date"]

#: The PDS (Personal Demographics Service) trace outcome columns behind
#: this table's quality score, given exact names rather than guessed —
#: see _find_exact_column(). NHSNumberNotFoundPDS is a flag column
#: (bad when true); the rest are match columns (bad when *not* true,
#: including NULL — a match that was never evaluated isn't a confirmed
#: match). "NHS number absent" isn't one of these — it's derived from
#: whichever NHS-number-like column NHS_NUMBER_PATTERNS finds instead,
#: since that column already has to be located for display purposes.
QUALITY_FLAG_COLUMNS = ["NHSNumberNotFoundPDS"]
QUALITY_MATCH_COLUMNS = ["birthDateMatch", "familyMatch", "genderMatch", "givenMatch", "postalCodeMatch"]
QUALITY_REASON_LABELS = {
    "NHSNumber_absent": "no NHS Number present",
    "NHSNumberNotFoundPDS": "NHS number not found in PDS",
    "birthDateMatch": "Date of birth doesn't match PDS",
    "familyMatch": "Surname doesn't match PDS",
    "genderMatch": "Gender doesn't match PDS",
    "givenMatch": "Forename doesn't match PDS",
    "postalCodeMatch": "Postcode doesn't match PDS",
}


def _find_column(columns, patterns):
    """First column (from table_columns()) whose name matches any of
    `patterns`, or None."""
    for col in columns:
        for pattern in patterns:
            if re.search(pattern, col["name"], re.IGNORECASE):
                return col
    return None


def _find_exact_column(columns, name):
    """The column (from table_columns()) whose name matches `name`
    case-insensitively, or None — for columns given an exact expected
    name (see QUALITY_FLAG_COLUMNS/QUALITY_MATCH_COLUMNS) rather than
    matched by best-effort pattern."""
    for col in columns:
        if col["name"].lower() == name.lower():
            return col
    return None


def _flag_true(value):
    """Best-effort truthy check for a bit/boolean-ish column value —
    this table's actual representation (1/0, 'Y'/'N', True/False, ...)
    isn't confirmed against a real server."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().upper() in {"1", "Y", "YES", "TRUE", "T"}


class IrisClient:
    """Thin wrapper around InterSystems' IRIS DB-API driver, scoped to
    read-only data-quality queries against RIE.PatientDemographics.
    Credentials are the same username/password used to log into the FHIR
    API — app.py's /data-quality route passes through the logged-in
    user's own client.user/client.password rather than asking for a
    second login, since both are confirmed to accept the same
    credentials on this deployment.

    build_report() scopes every query to a date range via
    self._date_filter_conditions/self._date_filter_params — set fresh at
    the start of each build_report() call (see _build_date_filter()) and
    read by every query-building helper below via _with_filter(). This
    is safe *only* because app.py constructs a brand new IrisClient per
    request rather than reusing one across requests (unlike FhirClient,
    which is deliberately cached per logged-in user) — don't cache/reuse
    an IrisClient across concurrent build_report() calls.
    """

    #: No hardcoded deployment-specific defaults for HOST/NAMESPACE — same
    #: reasoning as FHIR_BASE_URL in fhir_client.py: read purely from env
    #: vars and fail with a clear error at construction time (see
    #: __init__ below) rather than silently falling back to a value baked
    #: into source that's wrong for another deployment. PORT keeps a
    #: default since 1972 is IRIS's standard superserver port, not
    #: something deployment-specific.
    HOST = os.environ.get("IRIS_HOST")
    PORT = int(os.environ.get("IRIS_PORT", "1972"))
    NAMESPACE = os.environ.get("IRIS_NAMESPACE")

    TABLE_SCHEMA = "RIE"
    TABLE_NAME = "PatientDemographics"

    #: Cap on individual low/null-score entries fetched for the quality
    #: section's per-source listing — the per-source *counts* shown
    #: alongside it are a separate, uncapped GROUP BY query, so headline
    #: totals (and the overall/per-source reason breakdowns, which are
    #: computed from this same capped fetch) stay accurate for counts
    #: even if the entry listing itself is truncated; `truncated` says
    #: when that's happened.
    MAX_LOW_SCORE_ENTRIES = 2000

    def __init__(self, user, password, host=None, port=None, namespace=None):
        if iris_dbapi is None:
            raise RuntimeError(
                "intersystems-irispython isn't installed. Install it (see "
                "requirements.txt) — InterSystems distributes this driver "
                "from their own package index, not always plain PyPI, so "
                "`pip install intersystems-irispython` may need "
                "--extra-index-url if a plain install fails."
            )
        self.host = host or self.HOST
        if not self.host:
            raise ValueError(
                "IRIS_HOST is not set (and no host was passed in) — set the "
                "IRIS_HOST environment variable, e.g. 192.168.1.62"
            )
        self.namespace = namespace or self.NAMESPACE
        if not self.namespace:
            raise ValueError(
                "IRIS_NAMESPACE is not set (and no namespace was passed in) — "
                "set the IRIS_NAMESPACE environment variable, e.g. ENTERPRISESERVICEBUS"
            )
        self.port = port or self.PORT
        self.user = user
        self.password = password
        # Set for real at the start of each build_report() call — see
        # class docstring. Empty here just so _with_filter() has
        # something to read if ever called outside build_report().
        self._date_filter_conditions = []
        self._date_filter_params = []

    def connect(self):
        return iris_dbapi.connect(
            hostname=self.host, port=self.port, namespace=self.namespace,
            username=self.user, password=self.password,
        )

    @property
    def qualified_table(self):
        return f"{self.TABLE_SCHEMA}.{self.TABLE_NAME}"

    # ---- low-level helpers, all operating on a caller-supplied cursor
    # (build_report() opens one connection for the whole report rather
    # than one per query) ------------------------------------------------

    @staticmethod
    def _fetchall(cur, sql, params=None):
        cur.execute(sql, params or [])
        return cur.fetchall()

    @staticmethod
    def _fetchone(cur, sql, params=None):
        cur.execute(sql, params or [])
        row = cur.fetchone()
        return row[0] if row else None

    def _table_columns(self, cur):
        """[{"name", "type", "nullable"}, ...] for RIE.PatientDemographics,
        in column order — read from INFORMATION_SCHEMA rather than
        assumed, since this table's actual columns aren't confirmed."""
        sql = (
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
            "ORDER BY ORDINAL_POSITION"
        )
        rows = self._fetchall(cur, sql, [self.TABLE_SCHEMA, self.TABLE_NAME])
        return [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows]

    def _build_date_filter(self, last_updated_col, start, end):
        """(conditions, params) for the report's date-range filter —
        empty lists if there's no last-updated-like column, or no
        start/end was requested. `end` is treated as inclusive of the
        whole day (23:59:59) when the column's type is anything other
        than a bare DATE (i.e. it looks like it carries a time
        component); if the column really is DATE-only, appending a time
        would just be ignored, or worse, error, depending on the
        driver's date parsing, so it's only added when the column type
        suggests it means something."""
        if not last_updated_col or (not start and not end):
            return [], []
        name = last_updated_col["name"]
        is_date_only = (last_updated_col["type"] or "").upper() == "DATE"
        conditions, params = [], []
        if start:
            conditions.append(f'"{name}" >= ?')
            params.append(start)
        if end:
            conditions.append(f'"{name}" <= ?')
            params.append(end if is_date_only else f"{end} 23:59:59")
        return conditions, params

    def _with_filter(self, condition=None):
        """Combine the report's date-range filter (self._date_filter_*,
        set once per build_report() call) with an optional extra SQL
        condition into one WHERE clause. Returns (where_sql, params) —
        where_sql includes the leading " WHERE " (or "" if there's
        nothing to filter on at all). If `condition` itself contains a
        `?` placeholder, the caller must append that parameter's value
        after the returned `params` list, in the same order — the date
        filter's own params always come first since `condition` is
        always AND-ed on last."""
        parts = list(self._date_filter_conditions)
        if condition:
            parts.append(condition)
        if not parts:
            return "", list(self._date_filter_params)
        return " WHERE " + " AND ".join(parts), list(self._date_filter_params)

    def _row_count(self, cur):
        where_sql, params = self._with_filter()
        return self._fetchone(cur, f"SELECT COUNT(*) FROM {self.qualified_table}{where_sql}", params) or 0

    def _check_quality(self, cur, columns, nhs_col, mrn_col, surname_col, forename_col, dob_col,
                        postcode_col, source_col, score_col, threshold):
        """Rows with a low or null data-quality score, broken down by
        `source_col` (an ODS code identifying where the record came
        from) and by *why*: which of the PDS trace match/flag columns
        (see QUALITY_FLAG_COLUMNS/QUALITY_MATCH_COLUMNS) caused it, plus
        "NHS number absent" as an eighth reason derived from `nhs_col`.

        "Low or null" is deliberately `score_col <= threshold OR
        score_col IS NULL`, not just the threshold check alone: a record
        that PDS can't trace at all likely never gets a score computed
        for it, and `NULL <= threshold` is false in plain SQL — without
        the explicit NULL branch, exactly the records missing the
        identifier this app relies on most for patient matching (see
        "Patient matching" in CLAUDE.md) would silently vanish from the
        one report meant to surface them.

        Reason columns that aren't found on this table are simply
        skipped (best-effort — see module docstring); if none are found
        at all, every row's `reasons` list is empty and `reason_totals`
        is empty, but the by-source listing still works.

        Deliberately doesn't select or display a last-updated column at
        all: this table can carry more than one row per patient (e.g.
        re-traced against PDS on a different day) that's otherwise
        identical — with a last-updated timestamp in the mix those read
        as distinct rows, which showed up as duplicate-looking entries
        in the listing. Rows are de-duplicated per source on
        (`display_columns` values + computed `reasons`) so each distinct
        patient/reason combination is only counted and shown once,
        keeping the first occurrence in `"<source>", "<score>"` order.
        Every count in the result (`total_low_or_null`, `reason_totals`,
        each source's `count`) is derived from this de-duplicated set,
        not the raw row count — there's no separate "before de-dup"
        number shown anywhere, since that count isn't meaningful here.
        """
        reason_columns = []  # (key, label, sql_column_name, kind)
        if nhs_col:
            reason_columns.append(("NHSNumber_absent", QUALITY_REASON_LABELS["NHSNumber_absent"], nhs_col["name"], "absent"))
        for name in QUALITY_FLAG_COLUMNS:
            col = _find_exact_column(columns, name)
            if col:
                reason_columns.append((name, QUALITY_REASON_LABELS[name], col["name"], "true_is_bad"))
        for name in QUALITY_MATCH_COLUMNS:
            col = _find_exact_column(columns, name)
            if col:
                reason_columns.append((name, QUALITY_REASON_LABELS[name], col["name"], "false_is_bad"))

        where_sql, params = self._with_filter(f'("{score_col["name"]}" <= ? OR "{score_col["name"]}" IS NULL)')
        params = params + [threshold]

        display_columns = []
        for col in (nhs_col, mrn_col, surname_col, forename_col, dob_col, postcode_col,
                    source_col, score_col):
            if col and col["name"] not in display_columns:
                display_columns.append(col["name"])

        fetch_columns = list(display_columns)
        for _, _, sql_col, _ in reason_columns:
            if sql_col not in fetch_columns:
                fetch_columns.append(sql_col)

        col_list = ", ".join(f'"{c}"' for c in fetch_columns)
        entries_sql = (
            f'SELECT TOP {self.MAX_LOW_SCORE_ENTRIES} {col_list} FROM {self.qualified_table}{where_sql} '
            f'ORDER BY "{source_col["name"]}", "{score_col["name"]}"'
        )
        fetched_rows = self._fetchall(cur, entries_sql, params)

        reason_totals = {key: 0 for key, _, _, _ in reason_columns}
        reason_totals_by_source = {}
        by_source = {}
        seen_by_source = {}
        for row in fetched_rows:
            raw = dict(zip(fetch_columns, row))
            source_value = raw.get(source_col["name"]) or "Unknown"

            row_reasons = []
            for key, label, sql_col, kind in reason_columns:
                value = raw.get(sql_col)
                if kind == "absent":
                    is_bad = value in (None, "")
                elif kind == "true_is_bad":
                    is_bad = _flag_true(value)
                else:  # false_is_bad
                    is_bad = not _flag_true(value)
                if is_bad:
                    row_reasons.append((key, label))

            # If PDS never found a matching record at all, the individual
            # match columns (birthDateMatch/familyMatch/...) aren't
            # meaningful reasons in their own right — there was nothing
            # to match against — so they'd just be noise alongside the
            # real cause. Collapse down to that one reason instead of
            # listing all of them.
            if any(key == "NHSNumberNotFoundPDS" for key, _ in row_reasons):
                row_reasons = [(key, label) for key, label in row_reasons if key == "NHSNumberNotFoundPDS"]

            entry = {c: raw.get(c) for c in display_columns}
            entry["reasons"] = [label for _, label in row_reasons]

            dedup_key = tuple(entry.get(c) for c in display_columns) + tuple(entry["reasons"])
            seen = seen_by_source.setdefault(source_value, set())
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            source_reason_counts = reason_totals_by_source.setdefault(source_value, {})
            for key, label in row_reasons:
                reason_totals[key] += 1
                source_reason_counts[key] = source_reason_counts.get(key, 0) + 1

            by_source.setdefault(source_value, []).append(entry)

        total_low_or_null = sum(len(entries) for entries in by_source.values())

        reason_totals_list = sorted(
            (
                {"reason": label, "count": reason_totals[key]}
                for key, label, _, _ in reason_columns
                if reason_totals[key]
            ),
            key=lambda r: -r["count"],
        )

        sources_sorted = sorted(by_source, key=lambda s: (s == "Unknown", s))
        entries_by_source = [
            {
                "source": s,
                "count": len(by_source[s]),
                "reason_totals": sorted(
                    (
                        {"reason": label, "count": reason_totals_by_source.get(s, {}).get(key, 0)}
                        for key, label, _, _ in reason_columns
                        if reason_totals_by_source.get(s, {}).get(key)
                    ),
                    key=lambda r: -r["count"],
                ),
                "entries": by_source[s],
            }
            for s in sources_sorted
        ]

        return {
            "source_column": source_col["name"],
            "score_column": score_col["name"],
            "nhs_column": nhs_col["name"] if nhs_col else None,
            "reason_columns_detected": [label for _, label, _, _ in reason_columns],
            "threshold": threshold,
            "total_low_or_null": total_low_or_null,
            "reason_totals": reason_totals_list,
            "display_columns": display_columns,
            "entries_by_source": entries_by_source,
            "truncated": len(fetched_rows) >= self.MAX_LOW_SCORE_ENTRIES,
            "summary": (
                f"{total_low_or_null} unique row(s) with {score_col['name']} <= {threshold} or NULL, "
                f"in the selected date range, across {len(entries_by_source)} source(s)."
            ),
        }

    @staticmethod
    def _safe_check(title, fn, *args):
        """Runs `fn(*args)`, tagging the result with `title` — or, on any
        exception (wrong assumption about a column's type/contents,
        connectivity blip mid-report, etc.), returns an "ok": False entry
        carrying the error instead of raising, so a failed assumption
        shows as an error on the quality section rather than blanking
        the whole report."""
        try:
            return {"title": title, "ok": True, **fn(*args)}
        except Exception as e:
            return {"title": title, "ok": False, "error": str(e)}

    def build_report(self, start=None, end=None, score_threshold=8):
        """The full /data-quality page's data: schema-derived context
        (row count, date filtering) plus a single "quality" section
        focused on *why* rows have a low or null data-quality score,
        broken down by source (see _check_quality()). Only built when a
        source-like and score-like column are both found on this table
        (best-effort, see module docstring) — `report["quality"]` is
        None otherwise.

        `start`/`end` (date strings, "YYYY-MM-DD") scope every query in
        the report to a LastUpdated-like column when one is found by
        name — if none is found, the report runs unfiltered rather than
        silently ignoring the requested range, and `date_filtered: False`
        in the result says so. `score_threshold` is the score cutoff at
        or below which a row counts as low (independently of the NULL
        branch — see _check_quality()'s docstring).
        """
        conn = self.connect()
        try:
            cur = conn.cursor()
            try:
                columns = self._table_columns(cur)
                if not columns:
                    return {
                        "table": self.qualified_table,
                        "error": (
                            f"{self.qualified_table} not found, or has no columns "
                            "visible to this user in INFORMATION_SCHEMA.COLUMNS."
                        ),
                    }

                last_updated_col = _find_column(columns, LAST_UPDATED_PATTERNS)
                self._date_filter_conditions, self._date_filter_params = self._build_date_filter(
                    last_updated_col, start, end)

                total_rows = self._row_count(cur)

                nhs_col = _find_column(columns, NHS_NUMBER_PATTERNS)
                mrn_col = _find_column(columns, MRN_PATTERNS)
                surname_col = _find_column(columns, SURNAME_PATTERNS)
                forename_col = _find_column(columns, FORENAME_PATTERNS)
                dob_col = _find_column(columns, DOB_PATTERNS)
                postcode_col = _find_column(columns, POSTCODE_PATTERNS)
                source_col = _find_column(columns, SOURCE_PATTERNS)
                score_col = _find_column(columns, SCORE_PATTERNS)

                quality = None
                if source_col and score_col:
                    quality = self._safe_check(
                        "Quality", self._check_quality, cur, columns,
                        nhs_col, mrn_col, surname_col, forename_col, dob_col, postcode_col,
                        source_col, score_col, score_threshold)

                return {
                    "table": self.qualified_table,
                    "host": self.host,
                    "namespace": self.namespace,
                    "total_rows": total_rows,
                    "date_filtered": last_updated_col is not None,
                    "last_updated_column": last_updated_col["name"] if last_updated_col else None,
                    "start": start,
                    "end": end,
                    "score_threshold": score_threshold,
                    "quality": quality,
                }
            finally:
                cur.close()
        finally:
            conn.close()
