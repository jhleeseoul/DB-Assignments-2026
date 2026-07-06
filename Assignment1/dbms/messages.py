# 1-3 DML message helpers


def _msg_no_such_table(command_name: str) -> str:
    return f"{command_name} has failed: no such table"


def _msg_insert_type_mismatch() -> str:
    return "Insert has failed: types are not matched"


def _msg_insert_column_not_exist(col_name: str) -> str:
    return f"Insert has failed: '{col_name}' does not exist"


def _msg_insert_non_nullable(col_name: str) -> str:
    return f"Insert has failed: '{col_name}' is not nullable"


def _msg_update_result(count: int) -> str:
    return f"'{count}' row(s) updated"


def _msg_update_type_mismatch() -> str:
    return "Update has failed: types are not matched"


def _msg_update_column_not_exist(col_name: str) -> str:
    return f"Update has failed: '{col_name}' does not exist"


def _msg_update_non_nullable(col_name: str) -> str:
    return f"Update has failed: '{col_name}' is not nullable"


def _msg_delete_result(count: int) -> str:
    return f"'{count}' row(s) deleted"


def _msg_delete_ri_blocked(count: int) -> str:
    return f"'{count}' row(s) are not deleted due to referential integrity"


def _msg_select_table_not_exist(table_name: str) -> str:
    return f"Select has failed: '{table_name}' does not exist"


def _msg_select_column_resolve_error(col_name: str) -> str:
    return f"Select has failed: fail to resolve '{col_name}'"


def _msg_select_column_not_grouped(col_name: str) -> str:
    return (
        "Select has failed: column "
        f"'{col_name}' must either be included in the GROUP BY clause or be used in an aggregate function"
    )


def _msg_table_not_specified(clause_name: str) -> str:
    return f"{clause_name} clause trying to reference tables which are not specified"


def _msg_column_not_exist(clause_name: str) -> str:
    return f"{clause_name} clause trying to reference non existing column"


def _msg_ambiguous_reference(clause_name: str) -> str:
    return f"{clause_name} clause contains ambiguous column reference"


def _msg_incomparable() -> str:
    return "Trying to compare incomparable columns or values"


def _msg_invalid_limit_offset() -> str:
    return "Select has failed: LIMIT/OFFSET clause should be a non-negative integer"
