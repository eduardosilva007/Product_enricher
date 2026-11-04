#!/usr/bin/env python3
"""Fail-closed input -> final Excel integrity gate (no network/model/provider)."""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook


def _norm(value):
    if value is None:
        return ""
    return str(value).strip()


def _key(row):
    return (_norm(row.get("Marca")).casefold(), _norm(row.get("Modelo")).casefold())


def _read(path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        try:
            headers = [_norm(cell) for cell in next(iterator)]
        except StopIteration:
            raise ValueError(f"EMPTY_WORKBOOK:{path}")
        for required in ("Marca", "Modelo"):
            if required not in headers:
                raise ValueError(f"MISSING_COLUMN:{required}:{path}")
        if len(headers) != len(set(headers)):
            raise ValueError(f"DUPLICATE_HEADERS:{path}")
        rows = []
        for cells in iterator:
            data = dict(zip(headers, cells))
            if any(_norm(cell) for cell in cells):
                rows.append(data)
        return rows, set(headers)
    finally:
        workbook.close()


def validate(input_path, output_path):
    inputs, input_columns = _read(input_path)
    outputs, out_columns = _read(output_path)
    failures = []

    if not inputs:
        failures.append("EMPTY_INPUT")
    if len(inputs) != len(outputs):
        failures.append(f"ROW_COUNT:{len(inputs)}!={len(outputs)}")
    input_keys = Counter(_key(row) for row in inputs)
    output_keys = Counter(_key(row) for row in outputs)
    if input_keys != output_keys:
        missing = list((input_keys - output_keys).items())[:5]
        extra = list((output_keys - input_keys).items())[:5]
        failures.append(f"KEY_MULTIPLICITY:missing={missing}:extra={extra}")

    # A non-empty source title/URL provided in the input must be represented in
    # the same product's exported row. Use an exact key match and duplicate-aware
    # multiset matching to avoid silently comparing the wrong duplicate row.
    outputs_by_key = {}
    for row in outputs:
        outputs_by_key.setdefault(_key(row), []).append(row)

    title_cols = ("Título Real", "Título Referência")
    url_cols = ("Fonte URL", "Fonte Referência URL")
    evidence_cols = ("Resumo Evidências", "Fontes Pesquisa", "Evidence Extract")
    if not any(c in out_columns for c in title_cols):
        failures.append("NO_TITLE_COLUMNS")
    if not any(c in out_columns for c in url_cols):
        failures.append("NO_SOURCE_URL_COLUMNS")

    for key, in_count in input_keys.items():
        # Input records may be duplicates with differing evidence; match
        # preserved evidence against an unused output row for each key.
        input_group = [row for row in inputs if _key(row) == key]
        available = list(outputs_by_key.get(key, []))
        for source in input_group:
            expected_titles = {_norm(source.get(c)) for c in title_cols if _norm(source.get(c))}
            expected_urls = {_norm(source.get(c)) for c in url_cols if _norm(source.get(c))}
            expected_evidence = {_norm(source.get(c)) for c in evidence_cols if _norm(source.get(c))}
            matching = []
            for i, row in enumerate(available):
                actual_titles = {_norm(row.get(c)) for c in title_cols if _norm(row.get(c))}
                actual_urls = {_norm(row.get(c)) for c in url_cols if _norm(row.get(c))}
                actual_evidence = {_norm(row.get(c)) for c in evidence_cols if _norm(row.get(c))}
                if (expected_titles.issubset(actual_titles)
                        and expected_urls.issubset(actual_urls)
                        and expected_evidence.issubset(actual_evidence)):
                    matching.append(i)
            if matching:
                available.pop(matching[0])
            elif expected_titles or expected_urls or expected_evidence:
                failures.append(f"LOST_REFERENCE_DATA:{key}")

    # Observed reference title should not disappear from unresolved output:
    # check row-level consistency even when input did not carry source data.
    for row in outputs:
        reference = _norm(row.get("Título Referência"))
        real = _norm(row.get("Título Real"))
        if reference and not real and "Título Real" in out_columns:
            failures.append(f"REFERENCE_TITLE_NOT_IN_REAL_TITLE:{_key(row)}")
    return failures, len(inputs), len(outputs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--final-output", required=True)
    args = parser.parse_args()
    try:
        for filename in (args.input, args.final_output):
            if not Path(filename).is_file():
                raise ValueError(f"FILE_NOT_FOUND:{filename}")
        errors, count_in, count_out = validate(args.input, args.final_output)
    except Exception as exc:
        print(f"FINAL OUTPUT GATE: BLOCKED | {type(exc).__name__}: {exc}")
        return 2
    print(f"INPUT ROWS: {count_in} | FINAL ROWS: {count_out}")
    for failure in errors[:25]:
        print("FAIL:", failure)
    print("FINAL OUTPUT GATE:", "BLOCKED" if errors else "PASS")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
