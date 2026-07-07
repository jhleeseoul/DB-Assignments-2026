import lmdb

from .errors import DBError
from .expressions import Binding, ExpressionCompiler
from .messages import _msg_select_table_not_exist
from .plans import (
    Filter,
    GroupAggregate,
    LimitOffset,
    LogicalAggregate,
    LogicalFilter,
    LogicalJoin,
    LogicalLimitOffset,
    LogicalProject,
    LogicalScan,
    LogicalSort,
    NestedLoopJoin,
    Project,
    SelectPlan,
    Sort,
    TableScan,
)
from .storage import DBMS


def build_select_plan(
    parsed: dict,
    db: DBMS,
    txn: lmdb.Transaction,
    expressions: ExpressionCompiler,
) -> SelectPlan:
    table_refs = [parsed["from"]] + [join_item["table"] for join_item in parsed.get("joins", [])]
    bindings = _load_bindings(db, txn, table_refs)
    if not bindings:
        raise DBError("Syntax error")

    eval_ctx = expressions.build_eval_context(bindings)

    logical = LogicalScan(bindings[0])
    physical = TableScan(bindings[0])
    current_bindings = [bindings[0]]

    for join_item, join_binding in zip(parsed.get("joins", []), bindings[1:]):
        join_ctx = expressions.build_eval_context(current_bindings + [join_binding])
        logical_scan = LogicalScan(join_binding)
        physical_scan = TableScan(join_binding)
        logical = LogicalJoin(logical, logical_scan, join_item["on"], join_ctx)
        physical = NestedLoopJoin(physical, physical_scan, join_item["on"], join_ctx)
        current_bindings.append(join_binding)

    where_expr = parsed.get("where")
    if where_expr is not None:
        logical = LogicalFilter(logical, where_expr, eval_ctx)
        physical = Filter(physical, where_expr, eval_ctx)

    select_items = parsed["select"]
    has_group_by = parsed.get("group_by") is not None
    has_aggregate = _has_aggregate(select_items)
    if has_group_by or has_aggregate:
        logical = LogicalAggregate(logical, select_items, parsed.get("group_by"), eval_ctx)
        physical = GroupAggregate(physical, select_items, parsed.get("group_by"), eval_ctx)

    order_by = parsed.get("order_by")
    if order_by is not None:
        logical = LogicalSort(logical, order_by, eval_ctx)
        physical = Sort(physical, order_by, eval_ctx)

    limit_val = parsed.get("limit")
    offset_val = parsed.get("offset")
    if limit_val is not None or offset_val is not None:
        logical = LogicalLimitOffset(logical, limit_val, offset_val)
        physical = LimitOffset(physical, limit_val, offset_val)

    logical = LogicalProject(logical, select_items, eval_ctx)
    physical = Project(physical, select_items, eval_ctx)
    return SelectPlan(logical=logical, physical=physical, eval_ctx=eval_ctx, bindings=bindings)


def _load_bindings(db: DBMS, txn: lmdb.Transaction, table_refs: list[dict]) -> list[Binding]:
    bindings = []
    for ref in table_refs:
        table_name = db._to_key_text(ref["table"])
        alias_name = db._to_key_text(ref.get("alias") or table_name)
        meta = db._get_table_meta(txn, table_name)
        if meta is None:
            raise DBError(_msg_select_table_not_exist(table_name))
        bindings.append(
            Binding(
                table=table_name,
                alias=alias_name,
                meta=meta,
                col_index=db._column_index_map(meta),
                col_type=db._column_type_map(meta),
            )
        )
    return bindings


def _has_aggregate(select_items) -> bool:
    if select_items == "*":
        return False
    return any(item["expr"]["kind"] == "aggregate" for item in select_items)
