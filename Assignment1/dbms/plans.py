from dataclasses import dataclass, field
from typing import Any

from .expressions import Binding, EvalContext


class PlanNode:
    @property
    def name(self) -> str:
        return self.__class__.__name__

    @property
    def children(self) -> list["PlanNode"]:
        return []

    def detail(self) -> dict[str, Any]:
        return {}

    def shape(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name}
        detail = self.detail()
        if detail:
            data["detail"] = detail
        data["children"] = [child.shape() for child in self.children]
        return data


@dataclass
class SelectPlan:
    logical: PlanNode
    physical: PlanNode
    eval_ctx: EvalContext
    bindings: list[Binding]

    def physical_shape(self) -> dict[str, Any]:
        return self.physical.shape()


@dataclass
class LogicalScan(PlanNode):
    binding: Binding
    predicates: list[dict] = field(default_factory=list)

    def detail(self) -> dict[str, Any]:
        return {
            "table": self.binding.table,
            "alias": self.binding.alias,
            "predicate_count": len(self.predicates),
        }


@dataclass
class LogicalJoin(PlanNode):
    left: PlanNode
    right: PlanNode
    join_on: dict
    eval_ctx: EvalContext

    @property
    def children(self) -> list[PlanNode]:
        return [self.left, self.right]


@dataclass
class LogicalFilter(PlanNode):
    source: PlanNode
    predicate: dict
    eval_ctx: EvalContext

    @property
    def children(self) -> list[PlanNode]:
        return [self.source]


@dataclass
class LogicalAggregate(PlanNode):
    source: PlanNode
    select_items: Any
    group_by: Any
    eval_ctx: EvalContext

    @property
    def children(self) -> list[PlanNode]:
        return [self.source]


@dataclass
class LogicalSort(PlanNode):
    source: PlanNode
    order_by: Any
    eval_ctx: EvalContext

    @property
    def children(self) -> list[PlanNode]:
        return [self.source]


@dataclass
class LogicalLimitOffset(PlanNode):
    source: PlanNode
    limit: int | None
    offset: int | None

    @property
    def children(self) -> list[PlanNode]:
        return [self.source]


@dataclass
class LogicalProject(PlanNode):
    source: PlanNode
    select_items: Any
    eval_ctx: EvalContext

    @property
    def children(self) -> list[PlanNode]:
        return [self.source]


@dataclass
class TableScan(PlanNode):
    binding: Binding
    predicates: list[dict] = field(default_factory=list)

    def detail(self) -> dict[str, Any]:
        return {
            "table": self.binding.table,
            "alias": self.binding.alias,
            "predicate_count": len(self.predicates),
        }


@dataclass
class NestedLoopJoin(PlanNode):
    left: PlanNode
    right: TableScan
    join_on: dict
    eval_ctx: EvalContext

    @property
    def children(self) -> list[PlanNode]:
        return [self.left, self.right]


@dataclass
class Filter(PlanNode):
    source: PlanNode
    predicate: dict
    eval_ctx: EvalContext

    @property
    def children(self) -> list[PlanNode]:
        return [self.source]


@dataclass
class GroupAggregate(PlanNode):
    source: PlanNode
    select_items: Any
    group_by: Any
    eval_ctx: EvalContext

    @property
    def children(self) -> list[PlanNode]:
        return [self.source]


@dataclass
class Sort(PlanNode):
    source: PlanNode
    order_by: Any
    eval_ctx: EvalContext

    @property
    def children(self) -> list[PlanNode]:
        return [self.source]


@dataclass
class LimitOffset(PlanNode):
    source: PlanNode
    limit: int | None
    offset: int | None

    @property
    def children(self) -> list[PlanNode]:
        return [self.source]


@dataclass
class Project(PlanNode):
    source: PlanNode
    select_items: Any
    eval_ctx: EvalContext

    @property
    def children(self) -> list[PlanNode]:
        return [self.source]
