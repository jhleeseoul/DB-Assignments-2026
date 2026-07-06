import json
from pathlib import Path
from typing import Any

import lmdb

from .constraints import (
    has_primary_key_duplicate,
    row_has_foreign_key_violation,
    validate_date_literal,
)
from .errors import DBError
from .messages import (
    _msg_insert_column_not_exist,
    _msg_insert_non_nullable,
    _msg_insert_pk_duplicate,
    _msg_insert_referential_integrity,
    _msg_insert_type_mismatch,
    _msg_no_such_table,
)


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
                    validate_date_literal(value)
                    row_values[idx] = value
                else:
                    raise DBError(_msg_insert_type_mismatch())

            existing_rows = self._scan_rows(txn, table)
            if has_primary_key_duplicate(meta, row_values, existing_rows):
                raise DBError(_msg_insert_pk_duplicate())

            def get_parent_meta(parent_table: str):
                return self._get_table_meta(txn, parent_table)

            def get_parent_rows(parent_table: str):
                return self._scan_rows(txn, parent_table)

            if row_has_foreign_key_violation(meta, row_values, get_parent_meta, get_parent_rows):
                raise DBError(_msg_insert_referential_integrity())

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
