import ast

from lark import Lark, Transformer

from .config import DATE_LITERAL_TAG, EXIT_SIGNAL, NULL_LITERAL_TAG, PROJECT_ROOT


class CommandTransformer(Transformer):
    """Lark parse tree를 실행 가능한 AST(dict)로 변환한다."""

    def command(self, items):
        return items[0]

    def query(self, items):
        return items[0]

    def exit_query(self, _items):
        return {"kind": EXIT_SIGNAL}

    def create_table_query(self, items):
        table_name = self._first_identifier(items)
        elements = next((item for item in items if isinstance(item, list)), [])
        columns = []
        primary = []
        fks = []

        for elem in elements:
            if not isinstance(elem, dict):
                continue
            if elem["kind"] == "column":
                columns.append(elem)
            elif elem["kind"] == "primary":
                primary.append(elem["columns"])
            elif elem["kind"] == "foreign":
                fks.append(elem)

        return {
            "kind": "CREATE_TABLE",
            "table": table_name,
            "columns": columns,
            "primary": primary,
            "fks": [
                {
                    "columns": fk["columns"],
                    "ref_table": fk["ref_table"],
                    "ref_columns": fk["ref_columns"],
                }
                for fk in fks
            ],
        }

    def table_element_list(self, items):
        return [item for item in items if isinstance(item, dict)]

    def table_element(self, items):
        return items[0]

    def column_definition(self, items):
        column_name = items[0]
        type_spec = items[1]
        not_null = any(_is_keyword(it, "not") for it in items[2:])
        return {
            "kind": "column",
            "name": column_name,
            "type": self._normalize_type(type_spec["type"]),
            "char_len": type_spec.get("char_len"),
            "not_null": not_null,
        }

    def table_constraint_definition(self, items):
        return items[0]

    def primary_key_constraint(self, items):
        column_lists = [v for v in items if isinstance(v, list)]
        if not column_lists:
            return {"kind": "INVALID"}
        return {"kind": "primary", "columns": column_lists[0]}

    def referential_constraint(self, items):
        column_lists = [v for v in items if isinstance(v, list)]
        if len(column_lists) < 2:
            return {"kind": "INVALID"}
        table_names = [v for v in items if isinstance(v, str) and not self._is_reserved_keyword(v)]
        if not table_names:
            return {"kind": "INVALID"}
        return {
            "kind": "foreign",
            "columns": column_lists[0],
            "ref_table": table_names[-1],
            "ref_columns": column_lists[1],
        }

    def column_name_list(self, items):
        return [item for item in items if isinstance(item, str) and item not in {"(", ")"}]

    def data_type(self, items):
        type_name = items[0]
        if type_name == "char":
            char_len = next((token for token in items[1:] if isinstance(token, int)), None)
            return {"type": "char", "char_len": char_len}
        if type_name == "date":
            return {"type": "date", "char_len": None}
        return {"type": "int", "char_len": None}

    def drop_table_query(self, items):
        return {"kind": "DROP_TABLE", "table": self._first_identifier(items)}

    def explain_query(self, items):
        return {"kind": "EXPLAIN", "table": self._first_identifier(items)}

    def describe_query(self, items):
        return {"kind": "DESCRIBE", "table": self._first_identifier(items)}

    def desc_query(self, items):
        return {"kind": "DESC", "table": self._first_identifier(items)}

    def show_tables_query(self, _items):
        return {"kind": "SHOW_TABLES"}

    def truncate_table_query(self, items):
        return {"kind": "TRUNCATE_TABLE", "table": self._first_identifier(items)}

    def rename_table_query(self, items):
        for item in items:
            if isinstance(item, dict):
                return {"kind": "RENAME_TABLE", **item}
        return {"kind": "INVALID"}

    def rename_item(self, items):
        names = [
            item
            for item in items
            if isinstance(item, str) and not self._is_reserved_keyword(item)
        ]
        if len(names) < 2:
            return {"old_name": "", "new_name": ""}
        return {"old_name": names[0], "new_name": names[1]}

    def insert_query(self, items):
        table = self._first_identifier(items)
        list_items = [item for item in items if isinstance(item, list)]
        if not list_items or table is None:
            return {"kind": "INVALID"}
        values = list_items[-1]
        columns = None if len(list_items) == 1 else list_items[0]
        if len(list_items) > 2:
            return {"kind": "INVALID"}
        return {
            "kind": "INSERT",
            "table": table,
            "columns": columns,
            "values": values,
        }

    def value_list(self, items):
        return [item for item in items if isinstance(item, dict)]

    def delete_query(self, items):
        table = self._first_identifier(items)
        where_expr = next((item for item in items if isinstance(item, dict) and item.get("node") == "where"), None)
        return {
            "kind": "DELETE",
            "table": table,
            "where": None if where_expr is None else where_expr["expr"],
        }

    def update_query(self, items):
        table = self._first_identifier(items)
        assignments = next((item for item in items if isinstance(item, list)), [])
        where_expr = next((item for item in items if isinstance(item, dict) and item.get("node") == "where"), None)
        if table is None or not assignments:
            return {"kind": "INVALID"}
        return {
            "kind": "UPDATE",
            "table": table,
            "assignments": assignments,
            "where": None if where_expr is None else where_expr["expr"],
        }

    def set_clause(self, items):
        return [item for item in items if isinstance(item, dict) and item.get("node") == "set_item"]

    def set_item(self, items):
        column = next((item for item in items if isinstance(item, str) and not self._is_reserved_keyword(item)), None)
        value = next((item for item in items if isinstance(item, dict) and item.get("kind") == "literal"), None)
        if column is None or value is None:
            return {"node": "set_item", "column": "", "value": {"kind": "literal", "type": "null", "value": None}}
        return {"node": "set_item", "column": column, "value": value}

    def select_query(self, items):
        select_part = None
        from_part = None
        joins = []
        where_expr = None
        group_by = None
        order_by = None
        limit_val = None
        offset_val = None

        for item in items:
            if item == "*" or isinstance(item, list):
                if select_part is None:
                    select_part = item
                continue
            if isinstance(item, dict):
                tag = item.get("node")
                if tag == "from":
                    from_part = item["table"]
                elif tag == "join":
                    joins.append(item)
                elif tag == "where":
                    where_expr = item["expr"]
                elif tag == "group_by":
                    group_by = item["columns"]
                elif tag == "order_by":
                    order_by = item["items"]
                elif tag == "limit":
                    limit_val = item["value"]
                elif tag == "offset":
                    offset_val = item["value"]

        if select_part is None or from_part is None:
            return {"kind": "INVALID"}

        return {
            "kind": "SELECT",
            "select": select_part,
            "from": from_part,
            "joins": joins,
            "where": where_expr,
            "group_by": group_by,
            "order_by": order_by,
            "limit": limit_val,
            "offset": offset_val,
        }

    def select_list(self, items):
        if not items:
            return "*"
        if len(items) == 1 and items[0] == "*":
            return "*"
        return [item for item in items if isinstance(item, dict)]

    def select_item(self, items):
        alias = None
        names = [item for item in items if isinstance(item, str)]
        expr = next((item for item in items if isinstance(item, dict) and item.get("kind") in {"column", "aggregate"}), None)

        if expr is None:
            return {"kind": "INVALID"}

        if expr.get("kind") == "column":
            # column 자체에서 이름 문자열이 있으므로 alias는 마지막 문자열만 사용
            if len(names) >= 1:
                # names[0]은 column/table 이름들일 수 있어 alias는 마지막 값만 채택
                maybe_alias = names[-1]
                # table-qualified column에서 table/column 문자열과 alias가 섞이므로 AS 존재 시에만 alias 적용
                if any(_is_keyword(tok, "as") for tok in items):
                    alias = maybe_alias
        else:
            if names and any(_is_keyword(tok, "as") for tok in items):
                alias = names[-1]

        return {"expr": expr, "alias": alias}

    def aggregate_expr(self, items):
        func = next((item for item in items if isinstance(item, str) and item in {"max", "min", "sum", "count", "avg"}), None)
        aggregate_arg = next((item for item in items if isinstance(item, dict) and item.get("node") == "aggregate_arg"), None)
        col_ref = next((item for item in items if isinstance(item, dict) and item.get("kind") == "column"), None)
        if func is None:
            return {"kind": "INVALID"}
        if aggregate_arg is not None:
            if aggregate_arg.get("star"):
                return {"kind": "aggregate", "func": func, "star": True}
            return {"kind": "aggregate", "func": func, "column": aggregate_arg["column"]}
        if col_ref is None:
            return {"kind": "INVALID"}
        return {"kind": "aggregate", "func": func, "column": col_ref}

    def count_func(self, items):
        if not items:
            return None
        return items[0]

    def count_arg(self, items):
        col_ref = next((item for item in items if isinstance(item, dict) and item.get("kind") == "column"), None)
        if col_ref is None:
            return {"node": "aggregate_arg", "star": True}
        return {"node": "aggregate_arg", "star": False, "column": col_ref}

    def aggregate_func(self, items):
        if not items:
            return None
        return items[0]

    def from_clause(self, items):
        table_ref = next((item for item in items if isinstance(item, dict) and item.get("node") == "table_ref"), None)
        return {"node": "from", "table": table_ref}

    def table_reference(self, items):
        names = [item for item in items if isinstance(item, str)]
        if not names:
            return {"node": "table_ref", "table": "", "alias": ""}
        if len(names) == 1:
            table_name = names[0]
            alias_name = table_name
        else:
            table_name = names[0]
            alias_name = names[-1]
        return {"node": "table_ref", "table": table_name, "alias": alias_name}

    def join_clause(self, items):
        table_ref = next((item for item in items if isinstance(item, dict) and item.get("node") == "table_ref"), None)
        join_on = next((item for item in items if isinstance(item, dict) and item.get("node") == "join_on"), None)
        return {"node": "join", "table": table_ref, "on": join_on}

    def join_condition(self, items):
        refs = [item for item in items if isinstance(item, dict) and item.get("kind") == "column"]
        if len(refs) != 2:
            return {"node": "join_on", "left": {"kind": "column", "table": "", "column": ""}, "right": {"kind": "column", "table": "", "column": ""}}
        return {"node": "join_on", "left": refs[0], "right": refs[1]}

    def join_column_ref(self, items):
        names = [item for item in items if isinstance(item, str)]
        if len(names) < 2:
            return {"kind": "column", "table": "", "column": ""}
        return {"kind": "column", "table": names[0], "column": names[1]}

    def column_ref(self, items):
        names = [item for item in items if isinstance(item, str)]
        if len(names) >= 2:
            return {"kind": "column", "table": names[0], "column": names[1]}
        if len(names) == 1:
            return {"kind": "column", "table": None, "column": names[0]}
        return {"kind": "column", "table": None, "column": ""}

    def order_by_clause(self, items):
        order_items = [item for item in items if isinstance(item, dict) and item.get("node") == "order_item"]
        return {"node": "order_by", "items": order_items}

    def order_item(self, items):
        col_ref = next((item for item in items if isinstance(item, dict) and item.get("kind") == "column"), None)
        direction = next((item for item in items if isinstance(item, str) and item in {"asc", "desc"}), "asc")
        return {"node": "order_item", "column": col_ref, "direction": direction}

    def order_direction(self, items):
        if not items:
            return "asc"
        return items[0]

    def group_by_clause(self, items):
        col_refs = [item for item in items if isinstance(item, dict) and item.get("kind") == "column"]
        return {"node": "group_by", "columns": col_refs}

    def limit_clause(self, items):
        int_value = next((item for item in items if isinstance(item, int)), None)
        return {"node": "limit", "value": int_value}

    def offset_clause(self, items):
        int_value = next((item for item in items if isinstance(item, int)), None)
        return {"node": "offset", "value": int_value}

    def where_clause(self, items):
        expr = next((item for item in items if isinstance(item, dict) and item.get("type")), None)
        return {"node": "where", "expr": expr}

    def boolean_expr(self, items):
        if not items:
            return {"type": "comparison", "left": {}, "op": "=", "right": {}}
        expr = items[0]
        idx = 1
        while idx < len(items):
            op = items[idx]
            right = items[idx + 1]
            expr = {"type": op, "left": expr, "right": right}
            idx += 2
        return expr

    def boolean_term(self, items):
        if not items:
            return {"type": "comparison", "left": {}, "op": "=", "right": {}}
        expr = items[0]
        idx = 1
        while idx < len(items):
            op = items[idx]
            right = items[idx + 1]
            expr = {"type": op, "left": expr, "right": right}
            idx += 2
        return expr

    def boolean_factor(self, items):
        if len(items) == 1:
            return items[0]
        has_not = any(_is_keyword(item, "not") for item in items[:-1])
        target = items[-1]
        if has_not:
            return {"type": "not", "expr": target}
        return target

    def boolean_test(self, items):
        return items[0]

    def parenthesized_boolean_expr(self, items):
        expr = next((item for item in items if isinstance(item, dict) and item.get("type")), None)
        return expr

    def predicate(self, items):
        return items[0]

    def comparison_op(self, items):
        if not items:
            return "="
        return str(items[0])

    def comparison_predicate(self, items):
        left = items[0]
        op = items[1]
        right = items[2]
        return {"type": "comparison", "left": left, "op": op, "right": right}

    def comp_operand(self, items):
        item = items[0]
        if isinstance(item, dict) and item.get("kind") == "column":
            return item
        return item

    def comparable_value(self, items):
        if not items:
            return {"kind": "literal", "type": "null", "value": None}
        raw = items[0]

        if isinstance(raw, int):
            return {"kind": "literal", "type": "int", "value": raw}

        if isinstance(raw, tuple) and raw and raw[0] == DATE_LITERAL_TAG:
            return {"kind": "literal", "type": "date", "value": raw[1]}

        if isinstance(raw, tuple) and raw and raw[0] == NULL_LITERAL_TAG:
            return {"kind": "literal", "type": "null", "value": None}

        return {"kind": "literal", "type": "char", "value": raw}

    def null_predicate(self, items):
        col_ref = next((item for item in items if isinstance(item, dict) and item.get("kind") == "column"), None)
        negate = any(isinstance(item, dict) and item.get("node") == "null_op" and item.get("negate") for item in items)
        return {"type": "null", "column": col_ref, "negate": negate}

    def null_operation(self, items):
        negate = any(_is_keyword(item, "not") for item in items)
        return {"node": "null_op", "negate": negate}

    def table_name(self, items):
        return items[0]

    def column_name(self, items):
        return items[0]

    @staticmethod
    def _normalize_type(type_name: str) -> str:
        return str(type_name).lower()

    def IDENTIFIER(self, token):
        return str(token).lower()

    def TYPE_INT(self, _token):
        return "int"

    def TYPE_CHAR(self, _token):
        return "char"

    def TYPE_DATE(self, _token):
        return "date"

    def INT(self, token):
        return int(token)

    def STR(self, token):
        return ast.literal_eval(str(token))

    def DATE(self, token):
        return (DATE_LITERAL_TAG, str(token))

    def NULL(self, _token):
        return (NULL_LITERAL_TAG, None)

    def EQUAL(self, _token):
        return "="

    def NOTEQUAL(self, _token):
        return "!="

    def LESSTHAN(self, _token):
        return "<"

    def LESSEQUAL(self, _token):
        return "<="

    def GREATERTHAN(self, _token):
        return ">"

    def GREATEREQUAL(self, _token):
        return ">="

    def OR(self, _token):
        return "or"

    def AND(self, _token):
        return "and"

    def MAX(self, _token):
        return "max"

    def MIN(self, _token):
        return "min"

    def SUM(self, _token):
        return "sum"

    def COUNT(self, _token):
        return "count"

    def AVG(self, _token):
        return "avg"

    def ASC(self, _token):
        return "asc"

    def DESC(self, _token):
        return "desc"

    @staticmethod
    def _is_reserved_keyword(token: str) -> bool:
        if not isinstance(token, str):
            return False
        return token.lower() in {
            "create",
            "table",
            "drop",
            "rename",
            "to",
            "truncate",
            "describe",
            "explain",
            "desc",
            "show",
            "tables",
            "insert",
            "into",
            "values",
            "delete",
            "update",
            "set",
            "select",
            "from",
            "where",
            "limit",
            "offset",
            "key",
            "foreign",
            "references",
            "primary",
            "join",
            "on",
            "order",
            "by",
            "group",
            "count",
            "avg",
        }

    @classmethod
    def _first_identifier(cls, items):
        for item in items:
            if isinstance(item, str) and not cls._is_reserved_keyword(item):
                return item
        return None


def _is_keyword(value, keyword: str) -> bool:
    if isinstance(value, str):
        return value.lower() == keyword.lower()
    if hasattr(value, "value"):
        return str(value.value).lower() == keyword.lower()
    return False


def build_parser() -> Lark:
    grammar_path = PROJECT_ROOT / "grammar.lark"
    with grammar_path.open("r", encoding="utf-8") as grammar_file:
        return Lark(grammar_file.read(), start="command", lexer="basic")


def parse_statement(parser: Lark, transformer: CommandTransformer, statement: str):
    tree = parser.parse(statement)
    command = transformer.transform(tree)
    return command
