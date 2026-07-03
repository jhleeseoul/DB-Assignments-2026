from .config import EXIT_SIGNAL
from .executor import QueryExecutor
from .formatting import _format_result_set
from .messages import _msg_delete_result, _msg_delete_ri_blocked
from .storage import DBMS


def handle_create(db: DBMS, parsed: dict) -> list[str]:
    table = db.create_table(parsed)
    return [f"'{table}' table is created"]


def handle_drop(db: DBMS, parsed: dict) -> list[str]:
    dropped = db.drop_table(parsed["table"])
    return [f"'{dropped}' table is dropped"]


def handle_explain(db: DBMS, parsed: dict, command_name: str) -> list[str]:
    meta = db.explain(parsed["table"], command_name)
    columns = meta["columns"]

    header = ["column_name", "type", "null", "key"]
    rows = []
    for col in columns:
        if col["type"] == "int":
            col_type = "int"
        elif col["type"] == "date":
            col_type = "date"
        else:
            col_type = f"char({col['char_len']})"

        null_flag = "N" if col["not_null"] else "Y"
        if col["is_pk"] and col["is_fk"]:
            key = "PRI/FOR"
        elif col["is_pk"]:
            key = "PRI"
        elif col["is_fk"]:
            key = "FOR"
        else:
            key = ""
        rows.append([col["name"], col_type, null_flag, key])

    max_lengths = [
        max([len(header[i])] + [len(str(r[i])) for r in rows] if rows else [len(header[i])])
        for i in range(4)
    ]
    line = "-" * 60
    text_rows = [
        " | ".join(str(row[i]).ljust(max_lengths[i]) for i in range(4))
        for row in ([header] + rows)
    ]
    return [line] + text_rows + [line, f"{len(rows)} row{'s' if len(rows) != 1 else ''} in set"]


def handle_show_tables(db: DBMS) -> list[str]:
    tables = db.show_tables()
    line = "-" * 40
    return [line, *tables, line, f"{len(tables)} row{'s' if len(tables) != 1 else ''} in set"]


def handle_insert(db: DBMS, parsed: dict) -> list[str]:
    db.insert_into(parsed["table"], parsed.get("columns"), parsed["values"])
    return ["1 row inserted"]


def handle_delete(executor: QueryExecutor, parsed: dict) -> list[str]:
    result = executor.execute_delete(parsed)
    if result["blocked"]:
        return [_msg_delete_ri_blocked(result["count"])]
    return [_msg_delete_result(result["count"])]


def handle_select(executor: QueryExecutor, parsed: dict) -> list[str]:
    headers, rows = executor.execute_select(parsed)
    return _format_result_set(headers, rows)


def handle_rename(db: DBMS, parsed: dict) -> list[str]:
    new_name = db.rename_table(parsed["old_name"], parsed["new_name"])
    return [f"'{new_name}' is renamed"]


def handle_truncate(db: DBMS, parsed: dict) -> list[str]:
    table = db.truncate_table(parsed["table"])
    return [f"'{table}' is truncated"]


def dispatch(db: DBMS, executor: QueryExecutor, parsed: dict) -> list[str]:
    kind = parsed.get("kind")

    if kind == EXIT_SIGNAL:
        return []
    if kind == "CREATE_TABLE":
        return handle_create(db, parsed)
    if kind == "DROP_TABLE":
        return handle_drop(db, parsed)
    if kind in ("EXPLAIN", "DESCRIBE", "DESC"):
        return handle_explain(db, parsed, kind.title())
    if kind == "SHOW_TABLES":
        return handle_show_tables(db)
    if kind == "INSERT":
        return handle_insert(db, parsed)
    if kind == "DELETE":
        return handle_delete(executor, parsed)
    if kind == "SELECT":
        return handle_select(executor, parsed)
    if kind == "RENAME_TABLE":
        return handle_rename(db, parsed)
    if kind == "TRUNCATE_TABLE":
        return handle_truncate(db, parsed)
    if kind == "UPDATE":
        return ["Syntax error"]
    return ["Syntax error"]
