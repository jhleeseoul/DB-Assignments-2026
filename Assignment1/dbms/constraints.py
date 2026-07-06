from datetime import date
from typing import Any

from .errors import DBError
from .messages import _msg_invalid_date_value


def validate_date_literal(value: str) -> None:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise DBError(_msg_invalid_date_value()) from exc


def column_index_map(meta: dict) -> dict[str, int]:
    return {col["name"]: idx for idx, col in enumerate(meta["columns"])}


def key_tuple(row: list, columns: list[str], col_index: dict[str, int]) -> tuple[Any, ...]:
    return tuple(row[col_index[col]] for col in columns)


def primary_key_tuple(meta: dict, row: list) -> tuple[Any, ...] | None:
    pk_columns = meta.get("pk", [])
    if not pk_columns:
        return None
    return key_tuple(row, pk_columns, column_index_map(meta))


def foreign_key_tuple(meta: dict, fk: dict, row: list) -> tuple[Any, ...]:
    return key_tuple(row, fk.get("columns", []), column_index_map(meta))


def has_null_key(values: tuple[Any, ...]) -> bool:
    return any(value is None for value in values)


def row_matches_key(row: list, columns: list[str], expected: tuple[Any, ...], meta: dict) -> bool:
    return key_tuple(row, columns, column_index_map(meta)) == expected


def has_primary_key_duplicate(meta: dict, candidate_row: list, existing_rows: list[list]) -> bool:
    candidate_key = primary_key_tuple(meta, candidate_row)
    if candidate_key is None:
        return False
    for row in existing_rows:
        if primary_key_tuple(meta, row) == candidate_key:
            return True
    return False


def has_duplicate_primary_keys(meta: dict, rows: list[list]) -> bool:
    if not meta.get("pk"):
        return False
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        row_key = primary_key_tuple(meta, row)
        if row_key in seen:
            return True
        seen.add(row_key)
    return False


def has_referenced_parent(
    parent_meta: dict,
    parent_rows: list[list],
    ref_columns: list[str],
    ref_values: tuple[Any, ...],
) -> bool:
    for parent_row in parent_rows:
        if row_matches_key(parent_row, ref_columns, ref_values, parent_meta):
            return True
    return False


def row_has_foreign_key_violation(
    child_meta: dict,
    child_row: list,
    get_parent_meta,
    get_parent_rows,
) -> bool:
    for fk in child_meta.get("fks", []):
        child_values = foreign_key_tuple(child_meta, fk, child_row)
        if has_null_key(child_values):
            continue

        parent_meta = get_parent_meta(fk["ref_table"])
        if parent_meta is None:
            return True

        parent_rows = get_parent_rows(fk["ref_table"])
        if not has_referenced_parent(parent_meta, parent_rows, fk.get("ref_columns", []), child_values):
            return True
    return False
