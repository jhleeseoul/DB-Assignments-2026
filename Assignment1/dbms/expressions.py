from dataclasses import dataclass
from typing import Any, Callable

from .constraints import validate_date_literal
from .errors import DBError
from .messages import (
    _msg_ambiguous_reference,
    _msg_column_not_exist,
    _msg_incomparable,
    _msg_select_column_resolve_error,
    _msg_table_not_specified,
)


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


class ExpressionCompiler:
    def __init__(self, normalize_name: Callable[[str], str]) -> None:
        self.normalize_name = normalize_name

    def build_eval_context(self, bindings: list[Binding]) -> EvalContext:
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

    def resolve_column(self, ref: dict, ctx: EvalContext, clause: str, mode: str) -> ResolvedColumn:
        ref_table = ref.get("table")
        ref_col = self.normalize_name(ref["column"])

        if ref_table is not None:
            table_key = self.normalize_name(ref_table)
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

    def compile_boolean(
        self,
        expr: dict | None,
        ctx: EvalContext,
        clause: str,
    ) -> Callable[[dict[str, list]], bool]:
        if expr is None:
            return lambda _row_ctx: True

        node_type = expr.get("type")
        if node_type == "or":
            left_eval = self.compile_boolean(expr["left"], ctx, clause)
            right_eval = self.compile_boolean(expr["right"], ctx, clause)
            return lambda row_ctx: left_eval(row_ctx) or right_eval(row_ctx)

        if node_type == "and":
            left_eval = self.compile_boolean(expr["left"], ctx, clause)
            right_eval = self.compile_boolean(expr["right"], ctx, clause)
            return lambda row_ctx: left_eval(row_ctx) and right_eval(row_ctx)

        if node_type == "not":
            inner_eval = self.compile_boolean(expr["expr"], ctx, clause)
            return lambda row_ctx: not inner_eval(row_ctx)

        if node_type == "comparison":
            left_getter, left_type = self.compile_operand(expr["left"], ctx, clause)
            right_getter, right_type = self.compile_operand(expr["right"], ctx, clause)
            op = expr["op"]

            if not self.is_comparable(left_type, right_type, op):
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
            resolved = self.resolve_column(expr["column"], ctx, clause, mode="clause")
            negate = bool(expr.get("negate", False))

            if negate:
                return lambda row_ctx: row_ctx[resolved.alias][resolved.index] is not None
            return lambda row_ctx: row_ctx[resolved.alias][resolved.index] is None

        raise DBError("Syntax error")

    def compile_operand(
        self,
        operand: dict,
        ctx: EvalContext,
        clause: str,
    ) -> tuple[Callable[[dict[str, list]], Any], str]:
        if operand["kind"] == "literal":
            literal_value = operand["value"]
            literal_type = operand["type"]
            if literal_type == "date":
                validate_date_literal(literal_value)
            return (lambda _row_ctx, value=literal_value: value), literal_type

        resolved = self.resolve_column(operand, ctx, clause, mode="clause")
        return (lambda row_ctx, r=resolved: row_ctx[r.alias][r.index]), resolved.col_type

    @staticmethod
    def is_comparable(left_type: str, right_type: str, op: str) -> bool:
        if left_type == "null" or right_type == "null":
            return False
        if left_type != right_type:
            return False
        if left_type == "char":
            return op in {"=", "!="}
        if left_type in {"int", "date"}:
            return op in {"=", "!=", "<", "<=", ">", ">="}
        return False
