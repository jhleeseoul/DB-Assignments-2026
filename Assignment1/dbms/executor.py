from dataclasses import dataclass
from typing import Any, Callable

import lmdb

from .errors import DBError
from .messages import (
    _msg_ambiguous_reference,
    _msg_column_not_exist,
    _msg_incomparable,
    _msg_invalid_limit_offset,
    _msg_no_such_table,
    _msg_select_column_not_grouped,
    _msg_select_column_resolve_error,
    _msg_select_table_not_exist,
    _msg_table_not_specified,
    _msg_update_column_not_exist,
    _msg_update_non_nullable,
    _msg_update_type_mismatch,
)
from .storage import DBMS


@dataclass
class Binding:
    table: str
    alias: str
    meta: dict
    col_index: dict[str, int]
    col_type: dict[str, str]


@dataclass
class ResolvedColumn:
    alias: str
    column: str
    col_type: str
    index: int


@dataclass
class EvalContext:
    bindings: list[Binding]
    table_lookup: dict[str, Binding | None]
    unqualified: dict[str, list[ResolvedColumn]]


class QueryExecutor:
    def __init__(self, db: DBMS):
        self.db = db

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
            eval_ctx = self._build_eval_context([binding])
            predicate = self._compile_boolean(parsed.get("where"), eval_ctx, "Where")

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
            eval_ctx = self._build_eval_context([binding])
            predicate = self._compile_boolean(parsed.get("where"), eval_ctx, "Where")

            rows_with_keys = self.db._scan_rows_with_keys(txn, target_table)
            updated_count = 0
            for key, row in rows_with_keys:
                row_ctx = {binding.alias: row}
                if not predicate(row_ctx):
                    continue

                updated_row = list(row)
                for idx, value in prepared_assignments:
                    updated_row[idx] = value
                txn.put(key, self.db._to_json(updated_row), db=self.db.rows_db)
                updated_count += 1

            return updated_count

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
            return value

        raise DBError(_msg_update_type_mismatch())

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
            table_refs = [parsed["from"]] + [join_item["table"] for join_item in parsed.get("joins", [])]
            bindings = self._load_bindings(txn, table_refs)
            if not bindings:
                return [], []

            row_contexts = self._seed_base_rows(txn, bindings[0])
            current_bindings = [bindings[0]]
            for join_item, join_binding in zip(parsed.get("joins", []), bindings[1:]):
                row_contexts = self._apply_join(
                    txn,
                    row_contexts,
                    current_bindings,
                    join_binding,
                    join_item["on"],
                )
                current_bindings.append(join_binding)

            eval_ctx = self._build_eval_context(bindings)

            where_pred = self._compile_boolean(parsed.get("where"), eval_ctx, "Where")
            filtered_rows = [row_ctx for row_ctx in row_contexts if where_pred(row_ctx)]

            select_items = parsed["select"]
            has_group_by = parsed.get("group_by") is not None
            has_aggregate = self._has_aggregate(select_items)

            if select_items == "*" and (has_group_by or has_aggregate):
                raise DBError(_msg_select_column_not_grouped("*"))

            if not has_group_by and not has_aggregate:
                return self._execute_select_plain(
                    filtered_rows,
                    eval_ctx,
                    select_items,
                    parsed.get("order_by"),
                    limit_val,
                    offset_val,
                )

            return self._execute_select_grouped(
                filtered_rows,
                eval_ctx,
                select_items,
                parsed.get("group_by"),
                parsed.get("order_by"),
                limit_val,
                offset_val,
            )

    def _load_bindings(self, txn: lmdb.Transaction, table_refs: list[dict]) -> list[Binding]:
        bindings = []
        for ref in table_refs:
            table_name = self.db._to_key_text(ref["table"])
            alias_name = self.db._to_key_text(ref.get("alias") or table_name)
            meta = self.db._get_table_meta(txn, table_name)
            if meta is None:
                raise DBError(_msg_select_table_not_exist(table_name))
            bindings.append(
                Binding(
                    table=table_name,
                    alias=alias_name,
                    meta=meta,
                    col_index=self.db._column_index_map(meta),
                    col_type=self.db._column_type_map(meta),
                )
            )
        return bindings

    def _seed_base_rows(self, txn: lmdb.Transaction, binding: Binding) -> list[dict[str, list]]:
        rows = self.db._scan_rows(txn, binding.table)
        return [{binding.alias: row} for row in rows]

    def _apply_join(
        self,
        txn: lmdb.Transaction,
        current_rows: list[dict[str, list]],
        current_bindings: list[Binding],
        join_binding: Binding,
        join_on: dict,
    ) -> list[dict[str, list]]:
        join_rows = self.db._scan_rows(txn, join_binding.table)
        joined_bindings = current_bindings + [join_binding]
        eval_ctx = self._build_eval_context(joined_bindings)

        left_col = self._resolve_column(join_on["left"], eval_ctx, "Join", mode="join")
        right_col = self._resolve_column(join_on["right"], eval_ctx, "Join", mode="join")

        if left_col.col_type != right_col.col_type:
            raise DBError(_msg_incomparable())

        output_rows: list[dict[str, list]] = []
        for base_row_ctx in current_rows:
            for join_row in join_rows:
                merged = dict(base_row_ctx)
                merged[join_binding.alias] = join_row
                left_val = merged[left_col.alias][left_col.index]
                right_val = merged[right_col.alias][right_col.index]
                if left_val is None or right_val is None:
                    continue
                if left_val == right_val:
                    output_rows.append(merged)
        return output_rows

    def _build_eval_context(self, bindings: list[Binding]) -> EvalContext:
        table_lookup: dict[str, Binding | None] = {}
        unqualified: dict[str, list[ResolvedColumn]] = {}

        for binding in bindings:
            for key in (binding.alias, binding.table):
                if key not in table_lookup:
                    table_lookup[key] = binding
                elif table_lookup[key] is not binding:
                    table_lookup[key] = None

            for col_name, idx in binding.col_index.items():
                unqualified.setdefault(col_name, []).append(
                    ResolvedColumn(
                        alias=binding.alias,
                        column=col_name,
                        col_type=binding.col_type[col_name],
                        index=idx,
                    )
                )

        return EvalContext(bindings=bindings, table_lookup=table_lookup, unqualified=unqualified)

    def _resolve_column(self, ref: dict, ctx: EvalContext, clause: str, mode: str) -> ResolvedColumn:
        ref_table = ref.get("table")
        ref_col = self.db._to_key_text(ref["column"])

        if ref_table is not None:
            table_key = self.db._to_key_text(ref_table)
            binding = ctx.table_lookup.get(table_key)
            if binding is None:
                if mode == "select":
                    raise DBError(_msg_select_column_resolve_error(ref_col))
                if mode == "join":
                    raise DBError(_msg_column_not_exist("Join"))
                raise DBError(_msg_table_not_specified(clause))

            idx = binding.col_index.get(ref_col)
            if idx is None:
                if mode == "select":
                    raise DBError(_msg_select_column_resolve_error(ref_col))
                raise DBError(_msg_column_not_exist(clause))
            return ResolvedColumn(
                alias=binding.alias,
                column=ref_col,
                col_type=binding.col_type[ref_col],
                index=idx,
            )

        matches = ctx.unqualified.get(ref_col, [])
        if not matches:
            if mode == "select":
                raise DBError(_msg_select_column_resolve_error(ref_col))
            raise DBError(_msg_column_not_exist(clause))
        if len(matches) > 1:
            if mode == "select":
                raise DBError(_msg_select_column_resolve_error(ref_col))
            raise DBError(_msg_ambiguous_reference(clause))
        return matches[0]

    def _compile_boolean(
        self,
        expr: dict | None,
        ctx: EvalContext,
        clause: str,
    ) -> Callable[[dict[str, list]], bool]:
        if expr is None:
            return lambda _row_ctx: True

        node_type = expr.get("type")
        if node_type == "or":
            left_eval = self._compile_boolean(expr["left"], ctx, clause)
            right_eval = self._compile_boolean(expr["right"], ctx, clause)
            return lambda row_ctx: left_eval(row_ctx) or right_eval(row_ctx)

        if node_type == "and":
            left_eval = self._compile_boolean(expr["left"], ctx, clause)
            right_eval = self._compile_boolean(expr["right"], ctx, clause)
            return lambda row_ctx: left_eval(row_ctx) and right_eval(row_ctx)

        if node_type == "not":
            inner_eval = self._compile_boolean(expr["expr"], ctx, clause)
            return lambda row_ctx: not inner_eval(row_ctx)

        if node_type == "comparison":
            left_getter, left_type = self._compile_operand(expr["left"], ctx, clause)
            right_getter, right_type = self._compile_operand(expr["right"], ctx, clause)
            op = expr["op"]

            if not self._is_comparable(left_type, right_type, op):
                raise DBError(_msg_incomparable())

            def compare_eval(row_ctx: dict[str, list]) -> bool:
                left_val = left_getter(row_ctx)
                right_val = right_getter(row_ctx)
                if left_val is None or right_val is None:
                    return False
                if op == "=":
                    return left_val == right_val
                if op == "!=":
                    return left_val != right_val
                if op == "<":
                    return left_val < right_val
                if op == "<=":
                    return left_val <= right_val
                if op == ">":
                    return left_val > right_val
                if op == ">=":
                    return left_val >= right_val
                return False

            return compare_eval

        if node_type == "null":
            resolved = self._resolve_column(expr["column"], ctx, clause, mode="clause")
            negate = bool(expr.get("negate", False))

            if negate:
                return lambda row_ctx: row_ctx[resolved.alias][resolved.index] is not None
            return lambda row_ctx: row_ctx[resolved.alias][resolved.index] is None

        raise DBError("Syntax error")

    def _compile_operand(
        self,
        operand: dict,
        ctx: EvalContext,
        clause: str,
    ) -> tuple[Callable[[dict[str, list]], Any], str]:
        if operand["kind"] == "literal":
            literal_value = operand["value"]
            literal_type = operand["type"]
            return (lambda _row_ctx, value=literal_value: value), literal_type

        resolved = self._resolve_column(operand, ctx, clause, mode="clause")
        return (lambda row_ctx, r=resolved: row_ctx[r.alias][r.index]), resolved.col_type

    @staticmethod
    def _is_comparable(left_type: str, right_type: str, op: str) -> bool:
        if left_type == "null" or right_type == "null":
            return False
        if left_type != right_type:
            return False
        if left_type == "char":
            return op in {"=", "!="}
        if left_type in {"int", "date"}:
            return op in {"=", "!=", "<", "<=", ">", ">="}
        return False

    @staticmethod
    def _sort_value(value: Any) -> tuple[bool, Any]:
        return (value is None, value)

    @staticmethod
    def _order_items(order_by: Any) -> list[dict]:
        if order_by is None:
            return []
        if isinstance(order_by, dict):
            return [order_by]
        return list(order_by)

    def _has_aggregate(self, select_items: Any) -> bool:
        if select_items == "*":
            return False
        return any(item["expr"]["kind"] == "aggregate" for item in select_items)

    def _header_for_column_expr(self, expr: dict, alias_name: str | None = None) -> str:
        if alias_name:
            return alias_name
        if expr.get("table"):
            return f"{expr['table']}.{expr['column']}"
        return expr["column"]

    def _header_for_aggregate_expr(self, expr: dict, alias_name: str | None = None) -> str:
        if alias_name:
            return alias_name
        if expr.get("star"):
            return f"{expr['func']}(*)"
        col_ref = expr["column"]
        if col_ref.get("table"):
            target = f"{col_ref['table']}.{col_ref['column']}"
        else:
            target = col_ref["column"]
        return f"{expr['func']}({target})"

    def _sort_row_contexts(
        self,
        rows: list[dict[str, list]],
        eval_ctx: EvalContext,
        order_by: Any,
    ) -> list[dict[str, list]]:
        sorted_rows = rows
        for order_item in reversed(self._order_items(order_by)):
            order_col = self._resolve_column(order_item["column"], eval_ctx, "Order by", mode="clause")
            reverse = order_item["direction"] == "desc"
            sorted_rows = sorted(
                sorted_rows,
                key=lambda row_ctx, col=order_col: self._sort_value(row_ctx[col.alias][col.index]),
                reverse=reverse,
            )
        return sorted_rows

    def _execute_select_plain(
        self,
        filtered_rows: list[dict[str, list]],
        eval_ctx: EvalContext,
        select_items: Any,
        order_by: Any,
        limit_val: int | None,
        offset_val: int | None,
    ) -> tuple[list[str], list[list[Any]]]:
        if order_by is not None:
            filtered_rows = self._sort_row_contexts(filtered_rows, eval_ctx, order_by)

        start = offset_val or 0
        end = start + limit_val if limit_val is not None else None
        sliced_rows = filtered_rows[start:end]

        headers: list[str] = []
        output_rows: list[list[Any]] = []

        if select_items == "*":
            col_frequency: dict[str, int] = {}
            for binding in eval_ctx.bindings:
                for col in binding.meta["columns"]:
                    col_frequency[col["name"]] = col_frequency.get(col["name"], 0) + 1

            projections: list[ResolvedColumn] = []
            for binding in eval_ctx.bindings:
                for col in binding.meta["columns"]:
                    col_name = col["name"]
                    projections.append(
                        ResolvedColumn(
                            alias=binding.alias,
                            column=col_name,
                            col_type=binding.col_type[col_name],
                            index=binding.col_index[col_name],
                        )
                    )
                    if col_frequency[col_name] > 1:
                        headers.append(f"{binding.alias}.{col_name}")
                    else:
                        headers.append(col_name)

            for row_ctx in sliced_rows:
                output_rows.append([row_ctx[col.alias][col.index] for col in projections])
            return headers, output_rows

        projections: list[ResolvedColumn] = []
        for item in select_items:
            expr = item["expr"]
            if expr["kind"] != "column":
                raise DBError(_msg_select_column_not_grouped("*"))
            resolved = self._resolve_column(expr, eval_ctx, "Select", mode="select")
            projections.append(resolved)
            headers.append(self._header_for_column_expr(expr, item.get("alias")))

        for row_ctx in sliced_rows:
            output_rows.append([row_ctx[col.alias][col.index] for col in projections])
        return headers, output_rows

    def _execute_select_grouped(
        self,
        filtered_rows: list[dict[str, list]],
        eval_ctx: EvalContext,
        select_items: Any,
        group_by_ref: Any,
        order_by: Any,
        limit_val: int | None,
        offset_val: int | None,
    ) -> tuple[list[str], list[list[Any]]]:
        if select_items == "*":
            raise DBError(_msg_select_column_not_grouped("*"))

        group_refs = []
        if group_by_ref is not None:
            group_refs = group_by_ref if isinstance(group_by_ref, list) else [group_by_ref]

        group_cols: list[ResolvedColumn] = []
        if group_by_ref is not None:
            group_cols = [
                self._resolve_column(ref, eval_ctx, "Group by", mode="clause")
                for ref in group_refs
            ]
        group_col_keys = {(col.alias, col.index) for col in group_cols}

        headers: list[str] = []
        compiled_items = []
        has_aggregate = False

        for item in select_items:
            expr = item["expr"]
            if expr["kind"] == "column":
                resolved = self._resolve_column(expr, eval_ctx, "Select", mode="select")
                if not group_col_keys or (resolved.alias, resolved.index) not in group_col_keys:
                    raise DBError(_msg_select_column_not_grouped(expr["column"]))
                compiled_items.append({"kind": "column", "resolved": resolved, "expr": expr, "alias": item.get("alias")})
                headers.append(self._header_for_column_expr(expr, item.get("alias")))
            elif expr["kind"] == "aggregate":
                has_aggregate = True
                compiled = {
                    "kind": "aggregate",
                    "func": expr["func"],
                    "expr": expr,
                    "alias": item.get("alias"),
                }
                if expr.get("star"):
                    compiled["star"] = True
                else:
                    compiled["resolved"] = self._resolve_column(expr["column"], eval_ctx, "Select", mode="select")
                compiled_items.append(compiled)
                headers.append(self._header_for_aggregate_expr(expr, item.get("alias")))
            else:
                raise DBError("Syntax error")

        if not has_aggregate and not group_cols:
            return [], []

        groups: dict[Any, list[dict[str, list]]] = {}
        if not group_cols:
            groups["__all__"] = list(filtered_rows)
            if not filtered_rows:
                groups = {"__all__": []}
        else:
            for row_ctx in filtered_rows:
                key = tuple(row_ctx[col.alias][col.index] for col in group_cols)
                groups.setdefault(key, []).append(row_ctx)

        result_rows: list[dict[str, Any]] = []
        for _group_key, rows in groups.items():
            values: list[Any] = []
            representative = rows[0] if rows else None

            for item in compiled_items:
                if item["kind"] == "column":
                    if representative is None:
                        values.append(None)
                    else:
                        resolved = item["resolved"]
                        values.append(representative[resolved.alias][resolved.index])
                    continue

                func_name = item["func"]
                if item.get("star"):
                    if func_name == "count":
                        values.append(len(rows))
                        continue
                    raise DBError("Syntax error")

                resolved = item["resolved"]
                non_null_values = []
                for row_ctx in rows:
                    value = row_ctx[resolved.alias][resolved.index]
                    if value is not None:
                        non_null_values.append(value)

                if func_name == "count":
                    values.append(len(non_null_values))
                elif func_name == "avg":
                    if resolved.col_type != "int" or not non_null_values:
                        values.append(0)
                    else:
                        values.append(sum(non_null_values) / len(non_null_values))
                elif func_name == "max":
                    values.append(max(non_null_values) if non_null_values else None)
                elif func_name == "min":
                    values.append(min(non_null_values) if non_null_values else None)
                elif func_name == "sum":
                    if resolved.col_type != "int":
                        values.append(0)
                    else:
                        values.append(sum(non_null_values) if non_null_values else 0)
                else:
                    raise DBError("Syntax error")

            result_rows.append({"values": values, "representative": representative})

        if order_by is not None:
            for order_item in reversed(self._order_items(order_by)):
                order_col = self._resolve_column(order_item["column"], eval_ctx, "Order by", mode="clause")
                reverse = order_item["direction"] == "desc"
                result_rows = sorted(
                    result_rows,
                    key=lambda item, col=order_col: self._sort_value(
                        None
                        if item["representative"] is None
                        else item["representative"][col.alias][col.index]
                    ),
                    reverse=reverse,
                )

        start = offset_val or 0
        end = start + limit_val if limit_val is not None else None
        sliced = result_rows[start:end]

        return headers, [item["values"] for item in sliced]
