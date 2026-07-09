from dataclasses import dataclass
from typing import Any

import lmdb

from .errors import DBError
from .expressions import EvalContext, ExpressionCompiler, ResolvedColumn
from .messages import (
    _msg_incomparable,
    _msg_select_column_not_grouped,
)
from .plans import (
    Filter,
    GroupAggregate,
    LimitOffset,
    NestedLoopJoin,
    Project,
    Sort,
    TableScan,
)
from .storage import DBMS


@dataclass
class AggregatedRows:
    headers: list[str]
    rows: list[dict[str, Any]]


@dataclass
class ExecutionStats:
    scan_rows_read: int = 0
    scan_rows_emitted: int = 0
    join_comparisons: int = 0


class OperatorExecutor:
    def __init__(
        self,
        db: DBMS,
        txn: lmdb.Transaction,
        expressions: ExpressionCompiler,
        stats: ExecutionStats | None = None,
    ) -> None:
        self.db = db
        self.txn = txn
        self.expressions = expressions
        self.stats = stats

    def execute(self, node) -> tuple[list[str], list[list[Any]]] | list[dict[str, list]] | AggregatedRows:
        if isinstance(node, TableScan):
            return self._execute_table_scan(node)
        if isinstance(node, NestedLoopJoin):
            return self._execute_nested_loop_join(node)
        if isinstance(node, Filter):
            return self._execute_filter(node)
        if isinstance(node, GroupAggregate):
            return self._execute_group_aggregate(node)
        if isinstance(node, Sort):
            return self._execute_sort(node)
        if isinstance(node, LimitOffset):
            return self._execute_limit_offset(node)
        if isinstance(node, Project):
            return self._execute_project(node)
        raise DBError("Syntax error")

    def _execute_table_scan(self, node: TableScan) -> list[dict[str, list]]:
        rows = self.db._scan_rows(self.txn, node.binding.table)
        if self.stats is not None:
            self.stats.scan_rows_read += len(rows)

        eval_ctx = self.expressions.build_eval_context([node.binding])
        predicates = [
            self.expressions.compile_boolean(predicate, eval_ctx, "Where")
            for predicate in node.predicates
        ]

        output_rows: list[dict[str, list]] = []
        for row in rows:
            row_ctx = {node.binding.alias: row}
            if predicates and not all(predicate(row_ctx) for predicate in predicates):
                continue
            output_rows.append(row_ctx)

        if self.stats is not None:
            self.stats.scan_rows_emitted += len(output_rows)
        return output_rows

    def _execute_nested_loop_join(self, node: NestedLoopJoin) -> list[dict[str, list]]:
        left_rows = self.execute(node.left)
        right_rows = self.execute(node.right)
        if isinstance(left_rows, AggregatedRows) or isinstance(right_rows, AggregatedRows):
            raise DBError("Syntax error")

        left_col = self.expressions.resolve_column(node.join_on["left"], node.eval_ctx, "Join", mode="join")
        right_col = self.expressions.resolve_column(node.join_on["right"], node.eval_ctx, "Join", mode="join")

        if left_col.col_type != right_col.col_type:
            raise DBError(_msg_incomparable())

        output_rows: list[dict[str, list]] = []
        for left_ctx in left_rows:
            for right_ctx in right_rows:
                if self.stats is not None:
                    self.stats.join_comparisons += 1
                merged = dict(left_ctx)
                merged.update(right_ctx)
                left_val = merged[left_col.alias][left_col.index]
                right_val = merged[right_col.alias][right_col.index]
                if left_val is None or right_val is None:
                    continue
                if left_val == right_val:
                    output_rows.append(merged)
        return output_rows

    def _execute_filter(self, node: Filter) -> list[dict[str, list]]:
        rows = self.execute(node.source)
        if isinstance(rows, AggregatedRows):
            raise DBError("Syntax error")
        predicate = self.expressions.compile_boolean(node.predicate, node.eval_ctx, "Where")
        return [row_ctx for row_ctx in rows if predicate(row_ctx)]

    def _execute_group_aggregate(self, node: GroupAggregate) -> AggregatedRows:
        filtered_rows = self.execute(node.source)
        if isinstance(filtered_rows, AggregatedRows):
            raise DBError("Syntax error")

        select_items = node.select_items
        if select_items == "*":
            raise DBError(_msg_select_column_not_grouped("*"))

        group_refs = []
        if node.group_by is not None:
            group_refs = node.group_by if isinstance(node.group_by, list) else [node.group_by]

        group_cols: list[ResolvedColumn] = []
        if node.group_by is not None:
            group_cols = [
                self.expressions.resolve_column(ref, node.eval_ctx, "Group by", mode="clause")
                for ref in group_refs
            ]
        group_col_keys = {(col.alias, col.index) for col in group_cols}

        headers: list[str] = []
        compiled_items = []
        has_aggregate = False

        for item in select_items:
            expr = item["expr"]
            if expr["kind"] == "column":
                resolved = self.expressions.resolve_column(expr, node.eval_ctx, "Select", mode="select")
                if not group_col_keys or (resolved.alias, resolved.index) not in group_col_keys:
                    raise DBError(_msg_select_column_not_grouped(expr["column"]))
                compiled_items.append({"kind": "column", "resolved": resolved})
                headers.append(_header_for_column_expr(expr, item.get("alias")))
            elif expr["kind"] == "aggregate":
                has_aggregate = True
                compiled = {
                    "kind": "aggregate",
                    "func": expr["func"],
                }
                if expr.get("star"):
                    compiled["star"] = True
                else:
                    compiled["resolved"] = self.expressions.resolve_column(
                        expr["column"],
                        node.eval_ctx,
                        "Select",
                        mode="select",
                    )
                compiled_items.append(compiled)
                headers.append(_header_for_aggregate_expr(expr, item.get("alias")))
            else:
                raise DBError("Syntax error")

        if not has_aggregate and not group_cols:
            return AggregatedRows(headers=[], rows=[])

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

        return AggregatedRows(headers=headers, rows=result_rows)

    def _execute_sort(self, node: Sort):
        rows = self.execute(node.source)
        if isinstance(rows, AggregatedRows):
            result_rows = rows.rows
            for order_item in reversed(_order_items(node.order_by)):
                order_col = self.expressions.resolve_column(
                    order_item["column"],
                    node.eval_ctx,
                    "Order by",
                    mode="clause",
                )
                reverse = order_item["direction"] == "desc"
                result_rows = sorted(
                    result_rows,
                    key=lambda item, col=order_col: _sort_value(
                        None
                        if item["representative"] is None
                        else item["representative"][col.alias][col.index]
                    ),
                    reverse=reverse,
                )
            return AggregatedRows(headers=rows.headers, rows=result_rows)

        sorted_rows = rows
        for order_item in reversed(_order_items(node.order_by)):
            order_col = self.expressions.resolve_column(
                order_item["column"],
                node.eval_ctx,
                "Order by",
                mode="clause",
            )
            reverse = order_item["direction"] == "desc"
            sorted_rows = sorted(
                sorted_rows,
                key=lambda row_ctx, col=order_col: _sort_value(row_ctx[col.alias][col.index]),
                reverse=reverse,
            )
        return sorted_rows

    def _execute_limit_offset(self, node: LimitOffset):
        rows = self.execute(node.source)
        start = node.offset or 0
        end = start + node.limit if node.limit is not None else None
        if isinstance(rows, AggregatedRows):
            return AggregatedRows(headers=rows.headers, rows=rows.rows[start:end])
        return rows[start:end]

    def _execute_project(self, node: Project) -> tuple[list[str], list[list[Any]]]:
        rows = self.execute(node.source)
        if isinstance(rows, AggregatedRows):
            return rows.headers, [item["values"] for item in rows.rows]

        return _project_plain(rows, node.eval_ctx, node.select_items, self.expressions)


def _project_plain(
    rows: list[dict[str, list]],
    eval_ctx: EvalContext,
    select_items: Any,
    expressions: ExpressionCompiler,
) -> tuple[list[str], list[list[Any]]]:
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

        for row_ctx in rows:
            output_rows.append([row_ctx[col.alias][col.index] for col in projections])
        return headers, output_rows

    projections: list[ResolvedColumn] = []
    for item in select_items:
        expr = item["expr"]
        if expr["kind"] != "column":
            raise DBError(_msg_select_column_not_grouped("*"))
        resolved = expressions.resolve_column(expr, eval_ctx, "Select", mode="select")
        projections.append(resolved)
        headers.append(_header_for_column_expr(expr, item.get("alias")))

    for row_ctx in rows:
        output_rows.append([row_ctx[col.alias][col.index] for col in projections])
    return headers, output_rows


def _sort_value(value: Any) -> tuple[bool, Any]:
    return (value is None, value)


def _order_items(order_by: Any) -> list[dict]:
    if order_by is None:
        return []
    if isinstance(order_by, dict):
        return [order_by]
    return list(order_by)


def _header_for_column_expr(expr: dict, alias_name: str | None = None) -> str:
    if alias_name:
        return alias_name
    if expr.get("table"):
        return f"{expr['table']}.{expr['column']}"
    return expr["column"]


def _header_for_aggregate_expr(expr: dict, alias_name: str | None = None) -> str:
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
