from typing import Any

import lmdb

from .constraints import (
    has_duplicate_primary_keys,
    has_null_key,
    key_tuple,
    row_has_foreign_key_violation,
    validate_date_literal,
)
from .errors import DBError
from .expressions import Binding, ExpressionCompiler
from .messages import (
    _msg_invalid_limit_offset,
    _msg_no_such_table,
    _msg_update_column_not_exist,
    _msg_update_non_nullable,
    _msg_update_pk_duplicate,
    _msg_update_referential_integrity,
    _msg_update_type_mismatch,
)
from .operators import OperatorExecutor
from .planner import build_select_plan
from .storage import DBMS


class QueryExecutor:
    def __init__(self, db: DBMS):
        self.db = db
        self.expressions = ExpressionCompiler(self.db._to_key_text)

    def execute_delete(self, parsed: dict) -> dict[str, Any]:
        target_table = self.db._to_key_text(parsed["table"])

        with self.db.env.begin(write=True, buffers=False) as txn:
            meta = self.db._get_table_meta(txn, target_table)
            if meta is None:
                raise DBError(_msg_no_such_table("Delete"))

            binding = Binding(
                table=target_table,
                alias=target_table,
                meta=meta,
                col_index=self.db._column_index_map(meta),
                col_type=self.db._column_type_map(meta),
            )
            eval_ctx = self.expressions.build_eval_context([binding])
            predicate = self.expressions.compile_boolean(parsed.get("where"), eval_ctx, "Where")

            rows_with_keys = self.db._scan_rows_with_keys(txn, target_table)
            candidates = []
            for key, row in rows_with_keys:
                row_ctx = {binding.alias: row}
                if predicate(row_ctx):
                    candidates.append((key, row))

            requested_count = len(candidates)
            if requested_count == 0:
                return {"blocked": False, "count": 0}

            if self._is_delete_blocked_by_fk(txn, target_table, meta, candidates):
                return {"blocked": True, "count": requested_count}

            for key, _row in candidates:
                txn.delete(key, db=self.db.rows_db)

            meta["row_count"] = max(0, meta.get("row_count", 0) - requested_count)
            self.db._set_table_meta(txn, target_table, meta)
            return {"blocked": False, "count": requested_count}

    def execute_update(self, parsed: dict) -> int:
        target_table = self.db._to_key_text(parsed["table"])

        with self.db.env.begin(write=True, buffers=False) as txn:
            meta = self.db._get_table_meta(txn, target_table)
            if meta is None:
                raise DBError(_msg_no_such_table("Update"))

            col_index = self.db._column_index_map(meta)
            col_meta = self.db._column_meta_map(meta)
            prepared_assignments: list[tuple[int, Any]] = []
            assigned_columns: set[str] = set()

            for assignment in parsed.get("assignments", []):
                col_name = self.db._to_key_text(assignment["column"])
                if col_name in assigned_columns:
                    raise DBError(_msg_update_type_mismatch())
                assigned_columns.add(col_name)

                if col_name not in col_index:
                    raise DBError(_msg_update_column_not_exist(col_name))

                value = self._coerce_update_value(col_name, col_meta[col_name], assignment["value"])
                prepared_assignments.append((col_index[col_name], value))

            binding = Binding(
                table=target_table,
                alias=target_table,
                meta=meta,
                col_index=col_index,
                col_type=self.db._column_type_map(meta),
            )
            eval_ctx = self.expressions.build_eval_context([binding])
            predicate = self.expressions.compile_boolean(parsed.get("where"), eval_ctx, "Where")

            rows_with_keys = self.db._scan_rows_with_keys(txn, target_table)
            updated_rows: list[tuple[bytes, list, list]] = []
            for key, row in rows_with_keys:
                row_ctx = {binding.alias: row}
                if not predicate(row_ctx):
                    continue

                updated_row = list(row)
                for idx, value in prepared_assignments:
                    updated_row[idx] = value

                updated_rows.append((key, row, updated_row))

            if not updated_rows:
                return 0

            self._validate_update_constraints(
                txn,
                target_table,
                meta,
                rows_with_keys,
                updated_rows,
            )

            for key, _old_row, updated_row in updated_rows:
                txn.put(key, self.db._to_json(updated_row), db=self.db.rows_db)

            return len(updated_rows)

    def _coerce_update_value(self, col_name: str, col_meta: dict, literal: dict[str, Any]) -> Any:
        value_type = literal["type"]
        value = literal["value"]

        if value_type == "null":
            if col_meta["not_null"]:
                raise DBError(_msg_update_non_nullable(col_name))
            return None

        if col_meta["type"] == "int":
            if value_type != "int":
                raise DBError(_msg_update_type_mismatch())
            return int(value)

        if col_meta["type"] == "char":
            if value_type != "char":
                raise DBError(_msg_update_type_mismatch())
            text = str(value)
            if col_meta["char_len"] is not None and len(text) > col_meta["char_len"]:
                text = text[: col_meta["char_len"]]
            return text

        if col_meta["type"] == "date":
            if value_type != "date":
                raise DBError(_msg_update_type_mismatch())
            validate_date_literal(value)
            return value

        raise DBError(_msg_update_type_mismatch())

    def _validate_update_constraints(
        self,
        txn: lmdb.Transaction,
        target_table: str,
        target_meta: dict,
        rows_with_keys: list[tuple[bytes, list]],
        updated_rows: list[tuple[bytes, list, list]],
    ) -> None:
        updated_by_key = {key: updated_row for key, _old_row, updated_row in updated_rows}
        post_update_rows = [
            updated_by_key.get(key, row)
            for key, row in rows_with_keys
        ]

        if has_duplicate_primary_keys(target_meta, post_update_rows):
            raise DBError(_msg_update_pk_duplicate())

        def get_parent_meta(parent_table: str):
            return self.db._get_table_meta(txn, parent_table)

        def get_parent_rows(parent_table: str):
            if parent_table == target_table:
                return post_update_rows
            return self.db._scan_rows(txn, parent_table)

        for _key, _old_row, updated_row in updated_rows:
            if row_has_foreign_key_violation(
                target_meta,
                updated_row,
                get_parent_meta,
                get_parent_rows,
            ):
                raise DBError(_msg_update_referential_integrity())

        if self._is_update_blocked_by_referenced_fk(txn, target_table, target_meta, updated_rows):
            raise DBError(_msg_update_referential_integrity())

    def _is_update_blocked_by_referenced_fk(
        self,
        txn: lmdb.Transaction,
        target_table: str,
        target_meta: dict,
        updated_rows: list[tuple[bytes, list, list]],
    ) -> bool:
        target_col_idx = self.db._column_index_map(target_meta)

        for child_table in target_meta.get("referenced_by", []):
            child_meta = self.db._get_table_meta(txn, child_table)
            if child_meta is None:
                continue

            child_col_idx = self.db._column_index_map(child_meta)
            child_rows = self.db._scan_rows(txn, child_table)

            for fk in child_meta.get("fks", []):
                if fk.get("ref_table") != target_table:
                    continue

                parent_columns = self.db._fk_ref_columns(fk)
                child_columns = self.db._fk_columns(fk)
                if not parent_columns or not child_columns or len(parent_columns) != len(child_columns):
                    continue

                if any(col not in target_col_idx for col in parent_columns):
                    continue
                if any(col not in child_col_idx for col in child_columns):
                    continue

                child_ref_values = set()
                for child_row in child_rows:
                    child_tuple = key_tuple(child_row, child_columns, child_col_idx)
                    if has_null_key(child_tuple):
                        continue
                    child_ref_values.add(child_tuple)

                if not child_ref_values:
                    continue

                for _key, old_row, updated_row in updated_rows:
                    old_key = key_tuple(old_row, parent_columns, target_col_idx)
                    new_key = key_tuple(updated_row, parent_columns, target_col_idx)
                    if old_key == new_key or has_null_key(old_key):
                        continue
                    if old_key in child_ref_values:
                        return True

        return False

    def _is_delete_blocked_by_fk(
        self,
        txn: lmdb.Transaction,
        target_table: str,
        target_meta: dict,
        candidate_rows: list[tuple[bytes, list]],
    ) -> bool:
        target_col_idx = self.db._column_index_map(target_meta)

        for child_table in target_meta.get("referenced_by", []):
            child_meta = self.db._get_table_meta(txn, child_table)
            if child_meta is None:
                continue

            child_col_idx = self.db._column_index_map(child_meta)
            child_rows = self.db._scan_rows(txn, child_table)

            for fk in child_meta.get("fks", []):
                if fk.get("ref_table") != target_table:
                    continue

                parent_columns = self.db._fk_ref_columns(fk)
                child_columns = self.db._fk_columns(fk)
                if not parent_columns or not child_columns or len(parent_columns) != len(child_columns):
                    continue

                parent_indices = [target_col_idx.get(col) for col in parent_columns]
                child_indices = [child_col_idx.get(col) for col in child_columns]
                if any(idx is None for idx in parent_indices) or any(idx is None for idx in child_indices):
                    continue

                ref_values = set()
                for child_row in child_rows:
                    ref_tuple = tuple(child_row[idx] for idx in child_indices)
                    if any(value is None for value in ref_tuple):
                        continue
                    ref_values.add(ref_tuple)

                for _key, parent_row in candidate_rows:
                    parent_tuple = tuple(parent_row[idx] for idx in parent_indices)
                    if any(value is None for value in parent_tuple):
                        continue
                    if parent_tuple in ref_values:
                        return True
        return False

    def execute_select(self, parsed: dict) -> tuple[list[str], list[list[Any]]]:
        limit_val = parsed.get("limit")
        offset_val = parsed.get("offset")
        if limit_val is not None and limit_val < 0:
            raise DBError(_msg_invalid_limit_offset())
        if offset_val is not None and offset_val < 0:
            raise DBError(_msg_invalid_limit_offset())

        with self.db.env.begin(write=False, buffers=False) as txn:
            plan = build_select_plan(parsed, self.db, txn, self.expressions)
            headers, rows = OperatorExecutor(self.db, txn, self.expressions).execute(plan.physical)
            return headers, rows
