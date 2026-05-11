import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import lmdb
from lark import Lark, Transformer
from lark.exceptions import LarkError


STUDENT_ID = "2022-18758"
PROMPT = f"DB_{STUDENT_ID}> "
EXIT_SIGNAL = "__EXIT__"

PROJECT_ROOT = Path(__file__).resolve().parent
DB_DIR = PROJECT_ROOT / "DB"
DB_FILE = DB_DIR / "myDB.mdb"

DATE_LITERAL_TAG = "__DATE_LITERAL__"
NULL_LITERAL_TAG = "__NULL_LITERAL__"


class DBError(Exception):
    """DBMS 실행 중 비정상 상태를 표현한다."""


# 1-3 DML message helpers

def _msg_no_such_table(command_name: str) -> str:
    return f"{command_name} has failed: no such table"


def _msg_insert_type_mismatch() -> str:
    return "Insert has failed: types are not matched"


def _msg_insert_column_not_exist(col_name: str) -> str:
    return f"Insert has failed: '{col_name}' does not exist"


def _msg_insert_non_nullable(col_name: str) -> str:
    return f"Insert has failed: '{col_name}' is not nullable"


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


class DBMS:
    """LMDB 환경을 감싸는 핵심 데이터 접근 계층."""

    CATALOG_DB_NAME = b"catalog"
    ROWS_DB_NAME = b"rows"
    TABLE_LIST_KEY = b"__tables__"
    ROW_PREFIX = b"row:"
    TABLE_KEY_PREFIX = b"table:"
    TABLE_KEY_SEP = b":"
    ROW_ID_WIDTH = 20

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.env = lmdb.open(
            str(db_path),
            subdir=False,
            map_size=1 << 30,
            max_dbs=4,
            create=True,
            lock=True,
        )
        self.catalog_db = self.env.open_db(self.CATALOG_DB_NAME)
        self.rows_db = self.env.open_db(self.ROWS_DB_NAME)

    def close(self) -> None:
        self.env.close()

    @staticmethod
    def _to_key_text(name: str) -> str:
        return name.lower()

    def _table_key(self, table: str) -> bytes:
        return self.TABLE_KEY_PREFIX + table.encode("utf-8")

    def _table_key_with_db(self, table: str) -> bytes:
        return self.TABLE_KEY_PREFIX + table.encode("utf-8") + self.TABLE_KEY_SEP

    def _row_prefix(self, table: str) -> bytes:
        return f"{self.ROW_PREFIX.decode()}_{table}:".encode("utf-8")

    def _row_key(self, table: str, row_id: int) -> bytes:
        return (
            f"{self.ROW_PREFIX.decode()}_{table}:"
            f"{row_id:0{self.ROW_ID_WIDTH}d}".encode("utf-8")
        )

    @staticmethod
    def _to_json(value) -> bytes:
        return json.dumps(value, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _from_json(raw: bytes):
        return json.loads(raw.decode("utf-8"))

    def _get_table_list(self, txn: lmdb.Transaction) -> list[str]:
        raw = txn.get(self.TABLE_LIST_KEY, db=self.catalog_db)
        if raw is None:
            return []
        return list(self._from_json(raw))

    def _set_table_list(self, txn: lmdb.Transaction, tables: list[str]) -> None:
        txn.put(self.TABLE_LIST_KEY, self._to_json(tables), db=self.catalog_db)

    def _get_table_meta(self, txn: lmdb.Transaction, table: str):
        raw = txn.get(self._table_key(table), db=self.catalog_db)
        if raw is None:
            return None
        return self._from_json(raw)

    def _set_table_meta(self, txn: lmdb.Transaction, table: str, meta: dict) -> None:
        txn.put(self._table_key(table), self._to_json(meta), db=self.catalog_db)

    @staticmethod
    def _normalize_type_name(type_name: str) -> str:
        return type_name.lower()

    def _fk_columns(self, fk: dict) -> list[str]:
        raw_columns = fk.get("columns")
        if isinstance(raw_columns, list):
            return [self._to_key_text(col) for col in raw_columns if isinstance(col, str)]

        raw_column = fk.get("column")
        if isinstance(raw_column, str):
            return [self._to_key_text(raw_column)]
        return []

    def _fk_ref_columns(self, fk: dict) -> list[str]:
        raw_columns = fk.get("ref_columns")
        if isinstance(raw_columns, list):
            return [self._to_key_text(col) for col in raw_columns if isinstance(col, str)]

        raw_column = fk.get("ref_column")
        if isinstance(raw_column, str):
            return [self._to_key_text(raw_column)]
        return []

    def _normalize_column_constraint(self, table_meta: dict) -> None:
        pk_set = set(table_meta.get("pk", []))
        fk_set = set()
        for fk in table_meta.get("fks", []):
            fk_set.update(self._fk_columns(fk))
        for col in table_meta["columns"]:
            col["is_pk"] = col["name"] in pk_set
            col["is_fk"] = col["name"] in fk_set

    def _column_index_map(self, meta: dict) -> dict[str, int]:
        return {col["name"]: idx for idx, col in enumerate(meta["columns"])}

    def _column_type_map(self, meta: dict) -> dict[str, str]:
        return {col["name"]: col["type"] for col in meta["columns"]}

    def _column_meta_map(self, meta: dict) -> dict[str, dict]:
        return {col["name"]: col for col in meta["columns"]}

    def create_table(self, parsed: dict) -> str:
        table = self._to_key_text(parsed["table"])
        columns = parsed["columns"]
        pk_defs = parsed["primary"]
        fk_defs = parsed["fks"]

        if not isinstance(columns, list):
            raise DBError("Create table has failed: column definition is duplicated")

        with self.env.begin(write=True, buffers=False) as txn:
            tables = self._get_table_list(txn)
            if table in tables:
                raise DBError("Create table has failed: table with the same name already exists")

            col_names = []
            src_col_meta = {}
            for column in columns:
                c = self._to_key_text(column["name"])
                if c in col_names:
                    raise DBError("Create table has failed: column definition is duplicated")
                if column["type"] == "char" and (
                    column["char_len"] is None
                    or not isinstance(column["char_len"], int)
                    or column["char_len"] <= 0
                ):
                    raise DBError("Char length should be over 0")
                col_names.append(c)
                src_col_meta[c] = {
                    "type": self._normalize_type_name(column["type"]),
                    "char_len": column.get("char_len"),
                }

            if len(pk_defs) > 1:
                raise DBError("Create table has failed: primary key definition is duplicated")
            pk = pk_defs[0] if pk_defs else []
            pk = [self._to_key_text(x) for x in pk]

            col_set = set(col_names)
            for p in pk:
                if p not in col_set:
                    raise DBError(
                        f"Create table has failed:cannot define non-existing column '{p}' as primary key"
                    )

            prepared_fks = []
            for fk in fk_defs:
                fk_columns = [self._to_key_text(col) for col in fk.get("columns", [])]
                ref_columns = [self._to_key_text(col) for col in fk.get("ref_columns", [])]

                if not fk_columns or not ref_columns or len(fk_columns) != len(ref_columns):
                    raise DBError("Create table has failed: foreign key references non existing table or column")

                for col_name in fk_columns:
                    if col_name not in col_set:
                        raise DBError(
                            f"Create table has failed: cannot define non-existing column '{col_name}' as foreign key"
                        )

                ref_table = self._to_key_text(fk["ref_table"])
                ref_meta = self._get_table_meta(txn, ref_table)
                if ref_meta is None:
                    raise DBError("Create table has failed: foreign key references non existing table or column")

                ref_col_dict = {c["name"]: c for c in ref_meta["columns"]}
                for ref_column in ref_columns:
                    if ref_column not in ref_col_dict:
                        raise DBError("Create table has failed: foreign key references non existing table or column")

                ref_pk = [self._to_key_text(col) for col in ref_meta.get("pk", [])]
                if ref_columns != ref_pk:
                    raise DBError("Create table has failed: foreign key references non primary key column")

                for col_name, ref_column in zip(fk_columns, ref_columns):
                    src_type = src_col_meta[col_name]["type"]
                    src_char_len = src_col_meta[col_name]["char_len"]
                    tgt_type = ref_col_dict[ref_column]["type"]
                    tgt_char_len = ref_col_dict[ref_column]["char_len"]

                    if src_type != tgt_type:
                        raise DBError("Create table has failed: foreign key references wrong type")
                    if src_type == "char" and src_char_len != tgt_char_len:
                        raise DBError("Create table has failed: foreign key references wrong type")

                prepared_fks.append(
                    {
                        "columns": fk_columns,
                        "ref_table": ref_table,
                        "ref_columns": ref_columns,
                    }
                )

            meta = {
                "name": table,
                "columns": [
                    {
                        "name": self._to_key_text(c["name"]),
                        "type": self._normalize_type_name(c["type"]),
                        "char_len": c.get("char_len"),
                        "not_null": bool(c.get("not_null", False)),
                        "is_pk": False,
                        "is_fk": False,
                    }
                    for c in columns
                ],
                "pk": pk,
                "fks": prepared_fks,
                "referenced_by": [],
                "next_row_id": 0,
                "row_count": 0,
            }
            if pk:
                pk_set = set(pk)
                for col in meta["columns"]:
                    if col["name"] in pk_set:
                        col["not_null"] = True
            self._normalize_column_constraint(meta)

            for fk in prepared_fks:
                target_meta = self._get_table_meta(txn, fk["ref_table"])
                if target_meta is None:
                    continue
                referenced_by = target_meta.setdefault("referenced_by", [])
                if table not in referenced_by:
                    referenced_by.append(table)
                self._set_table_meta(txn, fk["ref_table"], target_meta)

            tables.append(table)
            self._set_table_list(txn, tables)
            self._set_table_meta(txn, table, meta)
            return table

    def drop_table(self, table_name: str) -> str:
        table = self._to_key_text(table_name)
        with self.env.begin(write=True, buffers=False) as txn:
            tables = self._get_table_list(txn)
            if table not in tables:
                raise DBError("Drop table has failed: no such table")

            meta = self._get_table_meta(txn, table)
            if not meta:
                raise DBError("Drop table has failed: no such table")

            if meta.get("referenced_by"):
                raise DBError(f"Drop table has failed: '{table}' is referenced by another table")

            for fk in meta.get("fks", []):
                target = fk["ref_table"]
                target_meta = self._get_table_meta(txn, target)
                if target_meta:
                    target_meta["referenced_by"] = [
                        t for t in target_meta.get("referenced_by", []) if t != table
                    ]
                    self._set_table_meta(txn, target, target_meta)

            self._delete_all_rows(txn, table)
            txn.delete(self._table_key(table), db=self.catalog_db)
            self._set_table_list(txn, [t for t in tables if t != table])
            return table

    def rename_table(self, old: str, new: str) -> str:
        old_name = self._to_key_text(old)
        new_name = self._to_key_text(new)

        with self.env.begin(write=True, buffers=False) as txn:
            tables = self._get_table_list(txn)
            if old_name not in tables:
                raise DBError("Rename table has failed: no such table")
            if new_name in tables:
                raise DBError(f"Rename table has failed: there is already a table named '{new_name}'")

            meta = self._get_table_meta(txn, old_name)
            if meta is None:
                raise DBError("Rename table has failed: no such table")

            self._move_rows_for_rename(txn, old_name, new_name)

            for tbl in tables:
                tmeta = self._get_table_meta(txn, tbl)
                if not tmeta:
                    continue
                changed = False
                for fk in tmeta.get("fks", []):
                    if fk["ref_table"] == old_name:
                        fk["ref_table"] = new_name
                        changed = True
                if changed:
                    self._set_table_meta(txn, tbl, tmeta)

            for fk in meta.get("fks", []):
                target_meta = self._get_table_meta(txn, fk["ref_table"])
                if target_meta is None:
                    continue
                referenced = target_meta.setdefault("referenced_by", [])
                referenced = [x for x in referenced if x != old_name]
                if new_name not in referenced:
                    referenced.append(new_name)
                target_meta["referenced_by"] = referenced
                self._set_table_meta(txn, fk["ref_table"], target_meta)

            for idx, t in enumerate(tables):
                if t == old_name:
                    tables[idx] = new_name
            self._set_table_list(txn, tables)

            meta["name"] = new_name
            self._set_table_meta(txn, new_name, meta)
            txn.delete(self._table_key(old_name), db=self.catalog_db)
            return new_name

    def truncate_table(self, table_name: str) -> str:
        table = self._to_key_text(table_name)
        with self.env.begin(write=True, buffers=False) as txn:
            tables = self._get_table_list(txn)
            if table not in tables:
                raise DBError("Truncate table has failed: no such table")

            meta = self._get_table_meta(txn, table)
            if meta is None:
                raise DBError("Truncate table has failed: no such table")

            if meta.get("referenced_by"):
                raise DBError(f"Truncate table has failed: '{table}' is referenced by another table")

            self._delete_all_rows(txn, table)
            meta["next_row_id"] = 0
            meta["row_count"] = 0
            self._set_table_meta(txn, table, meta)
            return table

    def insert_into(self, table_name: str, columns: list[str] | None, values: list[dict[str, Any]]) -> None:
        table = self._to_key_text(table_name)
        with self.env.begin(write=True, buffers=False) as txn:
            meta = self._get_table_meta(txn, table)
            if meta is None:
                raise DBError(_msg_no_such_table("Insert"))

            col_order = [c["name"] for c in meta["columns"]]
            col_meta_map = self._column_meta_map(meta)

            if columns is None:
                if len(values) != len(col_order):
                    raise DBError(_msg_insert_type_mismatch())
                row_literals = list(values)
            else:
                provided = [self._to_key_text(x) for x in columns]
                if len(provided) != len(values):
                    raise DBError(_msg_insert_type_mismatch())
                if len(set(provided)) != len(provided):
                    raise DBError(_msg_insert_type_mismatch())

                for name in provided:
                    if name not in col_meta_map:
                        raise DBError(_msg_insert_column_not_exist(name))

                row_literals = [{"kind": "literal", "type": "null", "value": None} for _ in col_order]
                for name, literal in zip(provided, values):
                    idx = col_order.index(name)
                    row_literals[idx] = literal

            if len(row_literals) != len(col_order):
                raise DBError(_msg_insert_type_mismatch())

            row_values: list[Any] = [None for _ in col_order]
            for idx, col_name in enumerate(col_order):
                col_meta = col_meta_map[col_name]
                literal = row_literals[idx]

                value_type = literal["type"]
                value = literal["value"]

                if value_type == "null":
                    if col_meta["not_null"]:
                        raise DBError(_msg_insert_non_nullable(col_name))
                    row_values[idx] = None
                    continue

                if col_meta["type"] == "int":
                    if value_type != "int":
                        raise DBError(_msg_insert_type_mismatch())
                    row_values[idx] = int(value)
                elif col_meta["type"] == "char":
                    if value_type != "char":
                        raise DBError(_msg_insert_type_mismatch())
                    text = str(value)
                    if col_meta["char_len"] is not None and len(text) > col_meta["char_len"]:
                        text = text[: col_meta["char_len"]]
                    row_values[idx] = text
                elif col_meta["type"] == "date":
                    if value_type != "date":
                        raise DBError(_msg_insert_type_mismatch())
                    row_values[idx] = value
                else:
                    raise DBError(_msg_insert_type_mismatch())

            row_id = meta.get("next_row_id", 0) + 1
            txn.put(self._row_key(table, row_id), self._to_json(row_values), db=self.rows_db)
            meta["next_row_id"] = row_id
            meta["row_count"] = meta.get("row_count", 0) + 1
            self._set_table_meta(txn, table, meta)

    def explain(self, table_name: str, command_name: str) -> dict:
        table = self._to_key_text(table_name)
        with self.env.begin(write=False, buffers=False) as txn:
            meta = self._get_table_meta(txn, table)
            if meta is None:
                raise DBError(f"{command_name} has failed: no such table")
            return meta

    def show_tables(self) -> list[str]:
        with self.env.begin(write=False, buffers=False) as txn:
            return self._get_table_list(txn)

    def _delete_all_rows(self, txn: lmdb.Transaction, table: str) -> int:
        prefix = self._row_prefix(table)
        cursor = txn.cursor(self.rows_db)
        deleted = 0
        to_delete: list[bytes] = []

        key = cursor.set_range(prefix)
        while key is not None and key is not False:
            current_key = cursor.key()
            if not current_key.startswith(prefix):
                break
            to_delete.append(bytes(current_key))
            key = cursor.next()

        for key_to_delete in to_delete:
            txn.delete(key_to_delete, db=self.rows_db)
            deleted += 1
        return deleted

    def _move_rows_for_rename(self, txn: lmdb.Transaction, old_name: str, new_name: str) -> int:
        old_prefix = self._row_prefix(old_name)
        new_prefix = self._row_prefix(new_name)
        cursor = txn.cursor(self.rows_db)

        moved = []
        key = cursor.set_range(old_prefix)
        while key is not None and key is not False:
            current_key = cursor.key()
            if not current_key.startswith(old_prefix):
                break
            moved.append((bytes(current_key), bytes(cursor.value())))
            key = cursor.next()

        for old_key, value in moved:
            suffix = old_key[len(old_prefix) :]
            new_key = new_prefix + suffix
            txn.put(new_key, value, db=self.rows_db)
            txn.delete(old_key, db=self.rows_db)
        return len(moved)

    def _scan_rows(self, txn: lmdb.Transaction, table: str) -> list[list]:
        prefix = self._row_prefix(table)
        cursor = txn.cursor(self.rows_db)
        rows = []
        key = cursor.set_range(prefix)
        while key is not None and key is not False:
            current_key = cursor.key()
            if not current_key.startswith(prefix):
                break
            rows.append(self._from_json(cursor.value()))
            key = cursor.next()
        return rows

    def _scan_rows_with_keys(self, txn: lmdb.Transaction, table: str) -> list[tuple[bytes, list]]:
        prefix = self._row_prefix(table)
        cursor = txn.cursor(self.rows_db)
        rows: list[tuple[bytes, list]] = []
        key = cursor.set_range(prefix)
        while key is not None and key is not False:
            current_key = cursor.key()
            if not current_key.startswith(prefix):
                break
            rows.append((bytes(current_key), self._from_json(cursor.value())))
            key = cursor.next()
        return rows


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
        col_ref = expr["column"]
        if col_ref.get("table"):
            target = f"{col_ref['table']}.{col_ref['column']}"
        else:
            target = col_ref["column"]
        return f"{expr['func']}({target})"

    def _execute_select_plain(
        self,
        filtered_rows: list[dict[str, list]],
        eval_ctx: EvalContext,
        select_items: Any,
        order_by: dict | None,
        limit_val: int | None,
        offset_val: int | None,
    ) -> tuple[list[str], list[list[Any]]]:
        if order_by is not None:
            order_col = self._resolve_column(order_by["column"], eval_ctx, "Order by", mode="clause")
            reverse = order_by["direction"] == "desc"
            filtered_rows = sorted(
                filtered_rows,
                key=lambda row_ctx: self._sort_value(row_ctx[order_col.alias][order_col.index]),
                reverse=reverse,
            )

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
        group_by_ref: dict | None,
        order_by: dict | None,
        limit_val: int | None,
        offset_val: int | None,
    ) -> tuple[list[str], list[list[Any]]]:
        if select_items == "*":
            raise DBError(_msg_select_column_not_grouped("*"))

        group_col = None
        if group_by_ref is not None:
            group_col = self._resolve_column(group_by_ref, eval_ctx, "Group by", mode="clause")

        headers: list[str] = []
        compiled_items = []
        has_aggregate = False

        for item in select_items:
            expr = item["expr"]
            if expr["kind"] == "column":
                resolved = self._resolve_column(expr, eval_ctx, "Select", mode="select")
                if group_col is None or resolved.alias != group_col.alias or resolved.index != group_col.index:
                    raise DBError(_msg_select_column_not_grouped(expr["column"]))
                compiled_items.append({"kind": "column", "resolved": resolved, "expr": expr, "alias": item.get("alias")})
                headers.append(self._header_for_column_expr(expr, item.get("alias")))
            elif expr["kind"] == "aggregate":
                has_aggregate = True
                resolved = self._resolve_column(expr["column"], eval_ctx, "Select", mode="select")
                compiled_items.append(
                    {
                        "kind": "aggregate",
                        "func": expr["func"],
                        "resolved": resolved,
                        "expr": expr,
                        "alias": item.get("alias"),
                    }
                )
                headers.append(self._header_for_aggregate_expr(expr, item.get("alias")))
            else:
                raise DBError("Syntax error")

        if not has_aggregate and group_col is None:
            return [], []

        groups: dict[Any, list[dict[str, list]]] = {}
        if group_col is None:
            groups["__all__"] = list(filtered_rows)
            if not filtered_rows:
                groups = {"__all__": []}
        else:
            for row_ctx in filtered_rows:
                key = row_ctx[group_col.alias][group_col.index]
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

                resolved = item["resolved"]
                func_name = item["func"]
                non_null_values = []
                for row_ctx in rows:
                    value = row_ctx[resolved.alias][resolved.index]
                    if value is not None:
                        non_null_values.append(value)

                if func_name == "max":
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
            order_col = self._resolve_column(order_by["column"], eval_ctx, "Order by", mode="clause")
            reverse = order_by["direction"] == "desc"

            result_rows = sorted(
                result_rows,
                key=lambda item: self._sort_value(
                    None
                    if item["representative"] is None
                    else item["representative"][order_col.alias][order_col.index]
                ),
                reverse=reverse,
            )

        start = offset_val or 0
        end = start + limit_val if limit_val is not None else None
        sliced = result_rows[start:end]

        return headers, [item["values"] for item in sliced]


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

    def update_query(self, _items):
        return {"kind": "UPDATE"}

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
                    group_by = item["column"]
                elif tag == "order_by":
                    order_by = {"column": item["column"], "direction": item["direction"]}
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
        expr = None
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
        func = next((item for item in items if isinstance(item, str) and item in {"max", "min", "sum"}), None)
        col_ref = next((item for item in items if isinstance(item, dict) and item.get("kind") == "column"), None)
        if func is None or col_ref is None:
            return {"kind": "INVALID"}
        return {"kind": "aggregate", "func": func, "column": col_ref}

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
        col_ref = next((item for item in items if isinstance(item, dict) and item.get("kind") == "column"), None)
        direction = next((item for item in items if isinstance(item, str) and item in {"asc", "desc"}), "asc")
        return {"node": "order_by", "column": col_ref, "direction": direction}

    def order_direction(self, items):
        if not items:
            return "asc"
        return items[0]

    def group_by_clause(self, items):
        col_ref = next((item for item in items if isinstance(item, dict) and item.get("kind") == "column"), None)
        return {"node": "group_by", "column": col_ref}

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
    grammar_path = Path(__file__).with_name("grammar.lark")
    with grammar_path.open("r", encoding="utf-8") as grammar_file:
        return Lark(grammar_file.read(), start="command", lexer="basic")


def extract_statements(buffer: str):
    statements = []
    start = 0
    in_single = False
    in_double = False
    escaped = False

    for index, char in enumerate(buffer):
        if escaped:
            escaped = False
            continue

        if char == "\\" and (in_single or in_double):
            escaped = True
            continue

        if char == "'" and not in_double:
            in_single = not in_single
            continue

        if char == '"' and not in_single:
            in_double = not in_double
            continue

        if char == ";" and not in_single and not in_double:
            statement = buffer[start : index + 1].strip()
            if statement:
                statements.append(statement)
            start = index + 1

    return statements, buffer[start:]


def parse_statement(parser: Lark, transformer: CommandTransformer, statement: str):
    tree = parser.parse(statement)
    command = transformer.transform(tree)
    return command


def _to_display_value(value) -> str:
    if value is None:
        return "null"
    return str(value)


def _format_result_set(headers: list[str], rows: list[list[Any]]) -> list[str]:
    line = "-" * 60
    output = [line]
    if headers:
        output.append(" | ".join(headers))
    for row in rows:
        output.append(" | ".join(_to_display_value(v) for v in row))
    output.append(line)
    output.append(f"{len(rows)} row{'s' if len(rows) != 1 else ''} in set")
    return output


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


def main():
    parser = build_parser()
    transformer = CommandTransformer()
    database = DBMS(DB_FILE)
    executor = QueryExecutor(database)

    buffer = ""
    try:
        while True:
            try:
                line = input(PROMPT if not buffer else "")
            except EOFError:
                break

            buffer = f"{buffer}\n{line}" if buffer else line
            statements, buffer = extract_statements(buffer)

            for statement in statements:
                try:
                    command = parse_statement(parser, transformer, statement)
                    if not isinstance(command, dict):
                        print(f"{PROMPT}Syntax error")
                        continue
                except (LarkError, ValueError):
                    print(f"{PROMPT}Syntax error")
                    continue

                if command["kind"] == EXIT_SIGNAL:
                    return
                if command["kind"] == "INVALID":
                    print(f"{PROMPT}Syntax error")
                    continue

                try:
                    output = dispatch(database, executor, command)
                    for out_line in output:
                        if out_line:
                            print(f"{PROMPT}{out_line}")
                except DBError as error:
                    print(f"{PROMPT}{error}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
