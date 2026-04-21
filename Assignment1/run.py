import ast
import json
from dataclasses import dataclass
from pathlib import Path

import lmdb
from lark import Lark, Transformer
from lark.exceptions import LarkError


STUDENT_ID = "2022-18758"
PROMPT = f"DB_{STUDENT_ID}> "
EXIT_SIGNAL = "__EXIT__"

# 과제 pdf 명시대로 DB 파일은 프로젝트 폴더 내부에 위치
# 단일 DB를 쓰므로 Assignment1/DB/myDB.mdb로 고정
PROJECT_ROOT = Path(__file__).resolve().parent
DB_DIR = PROJECT_ROOT / "DB"
DB_FILE = DB_DIR / "myDB.mdb"


class DBError(Exception):
    """DBMS 실행 중 비정상 상태를 표현한다."""


class DBMS:
    """LMDB 환경을 감싸는 핵심 데이터 접근 계층.

    LMDB 내에서는 두 개의 DB를 분리해 사용한다.
    - catalog DB: 테이블 목록, 컬럼 스키마, 메타데이터 저장
    - rows DB: 모든 테이블의 실제 row 데이터를 prefix 키로 구분해 저장
    """

    CATALOG_DB_NAME = b"catalog"
    ROWS_DB_NAME = b"rows"
    TABLE_LIST_KEY = b"__tables__"
    ROW_PREFIX = b"row:"
    TABLE_KEY_PREFIX = b"table:"
    TABLE_KEY_SEP = b":"
    ROW_ID_WIDTH = 20

    def __init__(self, db_path: Path) -> None:
        # DB 파일 경로가 아직 없을 때 부모 디렉터리부터 미리 만들어 둔다.
        # 없으면 LMDB open 시점에서 실패할 수 있다.
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
        # 프로그램 종료 시점에 DB 환경을 닫아 변경분을 확정하고 잠금 해제.
        self.env.close()

    @staticmethod
    def _to_key_text(name: str) -> str:
        return name.lower()

    def _table_key(self, table: str) -> bytes:
        # catalog에서 테이블 메타데이터를 읽고 쓸 때 쓰는 기본 키
        return self.TABLE_KEY_PREFIX + table.encode("utf-8")

    def _table_key_with_db(self, table: str) -> bytes:
        # 향후 확장용(현재는 직접 사용되지 않음). table prefix + 구분자 형태 키
        return self.TABLE_KEY_PREFIX + table.encode("utf-8") + self.TABLE_KEY_SEP

    def _row_prefix(self, table: str) -> bytes:
        # rows DB에서 특정 테이블 row를 범위 스캔할 때 사용하는 prefix
        return f"{self.ROW_PREFIX.decode()}_{table}:".encode("utf-8")

    def _row_key(self, table: str, row_id: int) -> bytes:
        # row_id를 고정 너비 문자열로 채워서 사전순 정렬 상태를 유지
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
        # 시작점: catalog의 "__tables__" 키 하나에 전체 테이블명을 JSON 리스트로 보관한다.
        raw = txn.get(self.TABLE_LIST_KEY, db=self.catalog_db)
        if raw is None:
            return []
        return list(self._from_json(raw))

    def _set_table_list(self, txn: lmdb.Transaction, tables: list[str]) -> None:
        txn.put(self.TABLE_LIST_KEY, self._to_json(tables), db=self.catalog_db)

    def _get_table_meta(self, txn: lmdb.Transaction, table: str):
        # 한 테이블의 전체 스키마/상태를 반환. 없으면 None.
        raw = txn.get(self._table_key(table), db=self.catalog_db)
        if raw is None:
            return None
        return self._from_json(raw)

    def _set_table_meta(self, txn: lmdb.Transaction, table: str, meta: dict) -> None:
        txn.put(self._table_key(table), self._to_json(meta), db=self.catalog_db)

    @staticmethod
    def _normalize_type_name(type_name: str) -> str:
        return type_name.lower()

    @staticmethod
    def _type_label(column_meta: dict) -> str:
        if column_meta["type"] == "char":
            return f"char({column_meta['char_len']})"
        return "int"

    def _normalize_column_constraint(self, table_meta: dict) -> None:
        # 내부 스키마(col dict)에는 is_pk, is_fk 플래그를 매 요청마다 최신화해 UI 출력에 사용한다.
        pk_set = set(table_meta.get("pk", []))
        fk_set = set(fk["column"] for fk in table_meta.get("fks", []))
        for col in table_meta["columns"]:
            col["is_pk"] = col["name"] in pk_set
            col["is_fk"] = col["name"] in fk_set

    def create_table(self, parsed: dict) -> str:
        # CREATE TABLE 처리 전체 흐름:
        # 1) 테이블/컬럼 중복 검사
        # 2) PK/FK 정의 유효성 검증(컬럼 존재, 타입 호환, 참조 대상 PK 여부)
        # 3) 메타데이터 저장 및 다른 테이블의 referenced_by 갱신
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

            # 컬럼명 중복 확인
            col_names = []
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

            # Primary key 정의 중복 확인
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

            # Foreign key 검증 (존재성/타입/참조 대상 PK)
            prepared_fks = []
            for fk in fk_defs:
                if len(fk["columns"]) != 1 or len(fk["ref_columns"]) != 1:
                    # 과제 제한상 단일 컬럼 FK만 처리
                    raise DBError("Create table has failed: foreign key references non existing table or column")

                col_name = self._to_key_text(fk["columns"][0])
                if col_name not in col_set:
                    raise DBError(
                        f"Create table has failed: cannot define non-existing column '{col_name}' as foreign key"
                    )

                ref_table = self._to_key_text(fk["ref_table"])
                ref_column = self._to_key_text(fk["ref_columns"][0])
                ref_meta = self._get_table_meta(txn, ref_table)
                if ref_meta is None:
                    raise DBError("Create table has failed: foreign key references non existing table or column")

                ref_col_dict = {c["name"]: c for c in ref_meta["columns"]}
                if ref_column not in ref_col_dict:
                    raise DBError("Create table has failed: foreign key references non existing table or column")

                if ref_column not in ref_meta.get("pk", []):
                    raise DBError("Create table has failed: foreign key references non primary key column")

                src_type = next(
                    c for c in columns if self._to_key_text(c["name"]) == col_name
                )["type"]
                src_char_len = next(
                    c for c in columns if self._to_key_text(c["name"]) == col_name
                )["char_len"]
                tgt_type = ref_col_dict[ref_column]["type"]
                tgt_char_len = ref_col_dict[ref_column]["char_len"]

                if src_type != tgt_type:
                    raise DBError("Create table has failed: foreign key references wrong type")
                if src_type == "char" and src_char_len != tgt_char_len:
                    raise DBError("Create table has failed: foreign key references wrong type")

                prepared_fks.append(
                    {
                        "column": col_name,
                        "ref_table": ref_table,
                        "ref_column": ref_column,
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
            # Primary key columns are automatically NOT NULL.
            if pk:
                pk_set = set(pk)
                for col in meta["columns"]:
                    if col["name"] in pk_set:
                        col["not_null"] = True
            self._normalize_column_constraint(meta)

            # 참조 대상의 referenced_by 업데이트
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
        # DROP TABLE은 실제 row까지 완전히 삭제한 뒤 카탈로그에서 메타데이터만 제거한다.
        # 다른 테이블이 참조 중인 테이블은 FK 제약으로 보존.
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

            # 이 테이블이 참조하고 있는 대상들의 referenced_by에서 제거
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
        # RENAME 처리 주요사항
        # catalog의 테이블 목록에서 이름 교체
        # rows DB의 row key prefix를 새 이름으로 이동
        # FK의 참조 대상/참조 목록을 전부 old -> new로 갱신
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

            # row key prefix를 새 이름으로 이동
            self._move_rows_for_rename(txn, old_name, new_name)

            # foreign key의 참조 대상 명칭 업데이트
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

            # 다른 테이블의 referenced_by 갱신
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

            # 테이블 목록 변경
            for idx, t in enumerate(tables):
                if t == old_name:
                    tables[idx] = new_name
            self._set_table_list(txn, tables)

            meta["name"] = new_name
            self._set_table_meta(txn, new_name, meta)
            txn.delete(self._table_key(old_name), db=self.catalog_db)
            return new_name

    def truncate_table(self, table_name: str) -> str:
        # TRUNCATE는 스키마는 유지하고 데이터만 지운 뒤,
        # next_row_id를 0으로 리셋해 새 INSERT 시 row_id를 1부터 재시작한다.
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

    def insert_into(self, table_name: str, columns: list[str] | None, values: list) -> None:
        # INSERT 수행 흐름 :
        # 대상 테이블 조회 및 컬럼-값 길이 맞춤
        # 타입 변환 및 char 길이 잘림
        # row_id 증가 후 JSON 직렬화해 rows DB에 저장
        table = self._to_key_text(table_name)
        with self.env.begin(write=True, buffers=False) as txn:
            meta = self._get_table_meta(txn, table)
            if meta is None:
                raise DBError("Insert has failed: no such table")

            col_order = [c["name"] for c in meta["columns"]]
            col_meta_map = {c["name"]: c for c in meta["columns"]}

            if columns is None:
                target_columns = col_order
                if len(values) != len(target_columns):
                    raise DBError("Insert has failed: no such table")
                row_values = list(values)
            else:
                # 지정 컬럼이 없는 경우 기본적으로 null 처리
                provided = [self._to_key_text(x) for x in columns]
                for name in provided:
                    if name not in col_meta_map:
                        raise DBError("Insert has failed: no such table")
                if len(provided) != len(values):
                    raise DBError("Insert has failed: no such table")

                row_values = [None for _ in col_order]
                for name, value in zip(provided, values):
                    idx = col_order.index(name)
                    row_values[idx] = value

            if len(row_values) != len(col_order):
                raise DBError("Insert has failed: no such table")

            # 타입 정합성 및 char 길이 조정
            for i, col_name in enumerate(col_order):
                meta_col = col_meta_map[col_name]
                val = row_values[i]
                if val is None:
                    continue

                if meta_col["type"] == "char":
                    value_text = str(val)
                    if meta_col["char_len"] is not None and len(value_text) > meta_col["char_len"]:
                        value_text = value_text[: meta_col["char_len"]]
                    row_values[i] = value_text
                elif meta_col["type"] == "int":
                    # 과제 조건상 정상 튜플을 가정한다.
                    row_values[i] = int(val)

            row_id = meta.get("next_row_id", 0) + 1
            txn.put(self._row_key(table, row_id), self._to_json(row_values), db=self.rows_db)
            meta["next_row_id"] = row_id
            meta["row_count"] = meta.get("row_count", 0) + 1
            self._set_table_meta(txn, table, meta)

    def select_all(self, table_name: str) -> tuple[list[str], list[list]]:
        # SELECT * 전용 조회: 현재 존재하는 row 전체를 입력 순서 그대로 반환
        table = self._to_key_text(table_name)
        with self.env.begin(write=False, buffers=False) as txn:
            meta = self._get_table_meta(txn, table)
            if meta is None:
                raise DBError(f"Select has failed: '{table}' does not exist")

            columns = [c["name"] for c in meta["columns"]]
            rows = self._scan_rows(txn, table)
            return columns, rows

    def explain(self, table_name: str, command_name: str) -> dict:
        # DESCRIBE/EXPLAIN/DESC는 같은 메타데이터 출력 형태를 사용한다.
        table = self._to_key_text(table_name)
        with self.env.begin(write=False, buffers=False) as txn:
            meta = self._get_table_meta(txn, table)
            if meta is None:
                raise DBError(f"{command_name} has failed: no such table")
            return meta

    def show_tables(self) -> list[str]:
        # 현재 존재하는 모든 테이블 목록을 반환한다.
        with self.env.begin(write=False, buffers=False) as txn:
            tables = self._get_table_list(txn)
            return tables

    def _delete_all_rows(self, txn: lmdb.Transaction, table: str) -> int:
        # 커서 위에서 즉시 삭제하면 탐색 순서가 깨질 수 있어
        # 삭제 대상 키만 먼저 수집한 뒤 일괄 삭제한다.
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
        # RENAME 시 rows DB의 키 패턴이 table 단위로 바뀌므로,
        # old_prefix에 일치하는 키를 모두 new_prefix로 이동한다.
        old_prefix = self._row_prefix(old_name)
        new_prefix = self._row_prefix(new_name)
        cursor = txn.cursor(self.rows_db)

        moved = []
        key = cursor.set_range(old_prefix)
        while key is not None and key is not False:
            current_key = cursor.key()
            if not current_key.startswith(old_prefix):
                break
            moved.append(
                (
                    bytes(current_key),
                    bytes(cursor.value()),
                )
            )
            key = cursor.next()

        for old_key, value in moved:
            suffix = old_key[len(old_prefix) :]
            new_key = new_prefix + suffix
            txn.put(new_key, value, db=self.rows_db)
            txn.delete(old_key, db=self.rows_db)
        return len(moved)

    def _scan_rows(self, txn: lmdb.Transaction, table: str) -> list[list]:
        # rows DB에서 특정 prefix를 범위 탐색하면서 JSON 행을 역직렬화해 반환.
        prefix = self._row_prefix(table)
        cursor = txn.cursor(self.rows_db)
        rows = []
        key = cursor.set_range(prefix)
        while key is not None and key is not False:
            current_key = cursor.key()
            if not current_key.startswith(prefix):
                break
            row_raw = cursor.value()
            rows.append(self._from_json(row_raw))
            key = cursor.next()
        return rows


@dataclass
class ParsedInsert:
    table: str
    columns: list[str] | None
    values: list


class CommandTransformer(Transformer):
    """Lark parse tree를 프로그램이 이해할 수 있는 AST(dict)로 변환한다.

    이 클래스는 SQL 문법 트리에서 쿼리 타입, 테이블명, 컬럼/값 리스트를
    추출해 dispatch 단계에서 바로 실행할 수 있는 형태로 통합한다.
    """

    def command(self, items):
        return items[0]

    def query(self, items):
        return items[0]

    def exit_query(self, _items):
        return {"kind": EXIT_SIGNAL}

    def create_table_query(self, items):
        # CREATE TABLE 내부에서 column/PK/FK 정의를 하나의 dict로 정규화.
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
        # col_name TYPE [NOT NULL] ... 형태를 AST 노드로 변환.
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
        # 과제 스펙에 맞게 단일 PK 컬럼 리스트만 허용.
        column_lists = [v for v in items if isinstance(v, list)]
        if not column_lists:
            return {"kind": "INVALID"}
        column_list = column_lists[0]
        return {"kind": "primary", "columns": column_list}

    def referential_constraint(self, items):
        # FOREIGN KEY <col list> REFERENCES <table> <col list>
        # FK/참조 대상이 모자라면 INVALID로 내려 DBMS에서 에러 처리한다.
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
        return [item for item in items if item is not None and str(item) not in {"(", ")"}]

    def data_type(self, items):
        # int/date/char(type_len) 파서를 공통 meta 형태로 바꾼다.
        type_name = items[0]
        if type_name == "char":
            char_len = None
            for token in items[1:]:
                if isinstance(token, int):
                    char_len = token
                    break
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
        # grammar은 여러 개 허용하지만 이번 과제에서 1회 rename만 지원
        for item in items:
            if isinstance(item, dict):
                return {"kind": "RENAME_TABLE", **item}
        return {"kind": "INVALID"}

    def rename_item(self, items):
        return {"old_name": items[0], "new_name": items[2]}

    def insert_query(self, items):
        # INSERT INTO table_name [column_list] VALUES value_list
        # 컬럼 리스트 유무에 따라 전체 컬럼 입력/부분 컬럼 입력을 구분한다.
        table = self._first_identifier(items)
        list_items = [item for item in items if isinstance(item, list)]
        if not list_items or table is None:
            return {"kind": "INVALID"}
        values = list_items[-1]
        columns = None if len(list_items) == 1 else list_items[0]
        if len(list_items) > 2:
            return {"kind": "INVALID"}
        if not isinstance(values, list):
            return {"kind": "INVALID"}
        if columns is not None and not isinstance(columns, list):
            return {"kind": "INVALID"}
        return {
            "kind": "INSERT",
            "table": table,
            "columns": columns,
            "values": values,
        }

    def value_list(self, items):
        return [item for item in items if item is not None and str(item) not in {"(", ")"}]

    def select_query(self, items):
        # select * from table_name only
        # 과제 요구사항 범위 밖(WHERE/LIMIT/OFFSET) 문법은 INVALID 처리.
        table_expr = None
        has_where = False
        star_token = None
        for item in items:
            if item is True:
                return {"kind": "INVALID"}
            if str(item) == "*":
                star_token = item
            if isinstance(item, dict) and "table" in item:
                table_expr = item
            if isinstance(item, dict) and item.get("has_where"):
                has_where = True

        if star_token != "*":
            return {"kind": "INVALID"}
        if table_expr is None or has_where:
            return {"kind": "INVALID"}
        return {"kind": "SELECT", "table": table_expr["table"]}

    def select_list(self, items):
        if not items:
            return "*"
        return items[0]

    def table_expression(self, items):
        # select from 절에서 table token만 분리해 "table + where유무" 형태로 전달.
        table = next(
            (item for item in items if item is not None and item is not True),
            None,
        )
        if isinstance(table, list):
            table = table[0] if table else None
        has_where = any(item is True for item in items)
        return {"table": table, "has_where": has_where}

    def table_reference_list(self, items):
        for item in items:
            if isinstance(item, str):
                return item
            if isinstance(item, list) and item:
                for sub in item:
                    if isinstance(sub, str):
                        return sub
        return None

    def referred_table(self, items):
        return items[0]

    def from_clause(self, items):
        # from절은 예약어를 제외한 실제 식별자를 하나 뽑아 사용.
        for item in items:
            if isinstance(item, str) and not self._is_reserved_keyword(item):
                return item
            if isinstance(item, str) and self._is_reserved_keyword(item):
                continue
            if hasattr(item, "value"):
                value = str(item.value).lower()
                if value and value not in {"from", "as"}:
                    return str(item.value)
            if isinstance(item, list) and item:
                return item[0]
        return None

    def where_clause(self, _items):
        return True

    def limit_clause(self, _items):
        return True

    def offset_clause(self, _items):
        return True

    def table_name(self, items):
        return items[0]

    def column_name(self, items):
        return items[0]

    def comparable_value(self, items):
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
        return str(token)

    def _coerce_bool_token(self, token):
        return str(token).lower()

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
            "select",
            "from",
            "where",
            "limit",
            "offset",
            "key",
            "foreign",
            "references",
            "primary",
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
    # grammar.lark 파일을 로드해 런타임 파서를 매번 새로 생성한다.
    grammar_path = Path(__file__).with_name("grammar.lark")
    with grammar_path.open("r", encoding="utf-8") as grammar_file:
        return Lark(grammar_file.read(), start="command", lexer="basic")


def extract_statements(buffer: str):
    # 세미콜론으로 종료되는 SQL 문장을 분리한다.
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
    # 입력 한 개 문장을 파싱 트리로 만들고 Transformer로 AST(dict)로 정규화한다.
    tree = parser.parse(statement)
    command = transformer.transform(tree)
    return command


def _to_display_value(value) -> str:
    # 출력 포맷팅에서 NULL 표현을 한 곳에서 통일한다.
    if value is None:
        return "null"
    return str(value)


def handle_create(db: DBMS, parsed: dict) -> list[str]:
    table = db.create_table(parsed)
    return [f"'{table}' table is created"]


def handle_drop(db: DBMS, parsed: dict) -> list[str]:
    table = parsed["table"]
    dropped = db.drop_table(table)
    return [f"'{dropped}' table is dropped"]


def handle_explain(db: DBMS, parsed: dict, command_name: str) -> list[str]:
    # EXPLAIN/DESCRIBE/DESC는 표 형식 문자열을 직접 조합해 콘솔 출력 포맷을 맞춘다.
    meta = db.explain(parsed["table"], command_name)
    columns = meta["columns"]

    header = ["column_name", "type", "null", "key"]
    rows = []
    for col in columns:
        col_type = "int" if col["type"] == "int" else f"char({col['char_len']})"
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
    return [line] + text_rows + [line, f"{len(rows)} rows in set"]


def handle_show_tables(db: DBMS) -> list[str]:
    # SHOW TABLES는 테이블명이 이미 [] 형태로 정렬되어 저장되므로
    # 그냥 줄바꿈 템플릿으로 감싸서 보여준다.
    tables = db.show_tables()
    line = "-" * 40
    return [line, *tables, line, f"{len(tables)} rows in set"]


def handle_insert(db: DBMS, parsed: ParsedInsert | dict) -> list[str]:
    db.insert_into(parsed["table"], parsed.get("columns"), parsed["values"])
    return ["The row is inserted"]


def handle_select(db: DBMS, parsed: dict) -> list[str]:
    # SELECT 출력은 컬럼 헤더 + 각 row를 문자열 행으로 변환한다.
    columns, rows = db.select_all(parsed["table"])
    if not columns:
        header_text = ""
    else:
        headers = [c.upper() for c in columns]
        line = "-" * 40
        out = [line, " | ".join(headers)]
        for row in rows:
            out.append(" | ".join(_to_display_value(v) for v in row))
        out.append(line)
        out.append(f"{len(rows)} row{'s' if len(rows) != 1 else ''} in set")
        return out
    return [f"{len(rows)} row{'s' if len(rows) != 1 else ''} in set"]


def handle_rename(db: DBMS, parsed: dict) -> list[str]:
    new_name = db.rename_table(parsed["old_name"], parsed["new_name"])
    return [f"'{new_name}' is renamed"]


def handle_truncate(db: DBMS, parsed: dict) -> list[str]:
    table = db.truncate_table(parsed["table"])
    return [f"'{table}' is truncated"]


def dispatch(db: DBMS, parsed: dict) -> list[str]:
    # AST kind별 처리 함수를 분기해 실제 동작과 에러 메시지 포맷을 분리한다.
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
    if kind == "SELECT":
        return handle_select(db, parsed)
    if kind == "RENAME_TABLE":
        return handle_rename(db, parsed)
    if kind == "TRUNCATE_TABLE":
        return handle_truncate(db, parsed)
    return ["Syntax error"]


def main():
    # 프로그램 시작 시 파서/변환기 준비, DB 오픈 후
    # 입력 버퍼에 ';' 기준으로 문장을 누적 파싱해 순차 실행한다.
    parser = build_parser()
    transformer = CommandTransformer()
    database = DBMS(DB_FILE)

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
                    # 문장을 AST로 변환
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
                    # AST에 따라 실제 DB 동작 호출
                    output = dispatch(database, command)
                    for line in output:
                        if line:
                            print(f"{PROMPT}{line}")
                except DBError as error:
                    # DB 규칙/무결성 오류는 예외로 일괄 변환해 메시지 출력
                    message = str(error)
                    print(f"{PROMPT}{message}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
