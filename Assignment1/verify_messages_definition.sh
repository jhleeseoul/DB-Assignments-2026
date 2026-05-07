#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_ACTIVATE="$BASE_DIR/.venv/bin/activate"
RUNNER="$BASE_DIR/run.py"
DB_FILE="$BASE_DIR/DB/myDB.mdb"
LOCK_FILE="${DB_FILE}-lock"

if [[ ! -f "$VENV_ACTIVATE" ]]; then
  echo "[ERR] virtualenv not found: $VENV_ACTIVATE"
  exit 1
fi
if [[ ! -f "$RUNNER" ]]; then
  echo "[ERR] runner not found: $RUNNER"
  exit 1
fi

source "$VENV_ACTIVATE"
rm -f "$DB_FILE" "$LOCK_FILE"

TMP_IN="$(mktemp)"
TMP_OUT="$(mktemp)"
trap 'rm -f "$TMP_IN" "$TMP_OUT"' EXIT

cat > "$TMP_IN" <<'SQL'
select * from ;
insert into no_t values(1);
delete from no_t;
create table msg_t (id int not null, txt char(3), d date);
insert into msg_t values(1, 'abc', 2026-01-01);
insert into msg_t values('x', 'abc', 2026-01-01);
insert into msg_t(no_col) values(1);
insert into msg_t values(null, 'abc', 2026-01-01);
create table m2 (id int, txt char(3));
insert into m2 values(1, 'ab');
insert into m2 values(2, 'cd');
delete from m2 where id = 2;
create table p (id int not null, primary key(id));
create table c (id int not null, pid int, primary key(id), foreign key(pid) references p(id));
insert into p values(1);
insert into c values(1, 1);
delete from p where id = 1;
select * from no_select;
create table s1 (id int, v int);
create table s2 (id int, w int);
insert into s1 values(1, 10);
insert into s2 values(1, 20);
select id from s1 join s2 on s1.id = s2.id;
select s1.id from s1 join s2 on s1.id = s2.id where t9.id = 1;
select s1.id from s1 join s2 on s1.id = s2.id where no_col = 1;
select s1.id from s1 join s2 on s1.id = s2.id where id = 1;
select id from msg_t where txt > 'a';
select * from msg_t order by id asc limit -1;
create table g1 (id int, grp char(1), v int);
insert into g1 values(1, 'A', 10);
insert into g1 values(2, 'A', 20);
select g1.grp, g1.id, sum(g1.v) from g1 group by g1.grp;
exit;
SQL

python "$RUNNER" < "$TMP_IN" > "$TMP_OUT" 2>&1

EXPECTED=(
  "SyntaxError|Syntax error"
  "NoSuchTableInsert|Insert has failed: no such table"
  "NoSuchTableDelete|Delete has failed: no such table"
  "InsertResult|1 row inserted::8"
  "InsertTypeMismatchError|Insert has failed: types are not matched"
  "InsertColumnExistenceError|Insert has failed: 'no_col' does not exist"
  "InsertColumnNonNullableError|Insert has failed: 'id' is not nullable"
  "DeleteResult|'1' row(s) deleted"
  "DeleteReferentialIntegrityPassed|'1' row(s) are not deleted due to referential integrity"
  "SelectTableExistenceError|Select has failed: 'no_select' does not exist"
  "SelectColumnResolveError|Select has failed: fail to resolve 'id'"
  "SelectColumnNotGrouped|Select has failed: column 'id' must either be included in the GROUP BY clause or be used in an aggregate function"
  "TableNotSpecified|Where clause trying to reference tables which are not specified"
  "ColumnNotExist|Where clause trying to reference non existing column"
  "AmbiguousReference|Where clause contains ambiguous column reference"
  "IncomparableError|Trying to compare incomparable columns or values"
  "InvalidLimitOffsetError|Select has failed: LIMIT/OFFSET clause should be a non-negative integer"
)

FAILED=0
PASSED=0

for item in "${EXPECTED[@]}"; do
  name="${item%%|*}"
  spec="${item#*|}"
  pattern="$spec"
  need=1
  if [[ "$spec" == *"::"* ]]; then
    pattern="${spec%%::*}"
    need="${spec##*::}"
  fi

  got="$(grep -F -c -- "$pattern" "$TMP_OUT" || true)"
  if [[ "$got" -ge "$need" ]]; then
    echo "[OK] $name"
    PASSED=$((PASSED + 1))
  else
    echo "[MISS] $name => $pattern (need=$need, got=$got)"
    FAILED=$((FAILED + 1))
  fi
done

echo
if [[ "$FAILED" -eq 0 ]]; then
  echo "[RESULT] PASS: $PASSED/${#EXPECTED[@]}"
  exit 0
fi

echo "[RESULT] FAIL: $FAILED/${#EXPECTED[@]}"
echo "--- runner output (for debug) ---"
sed 's/^/[OUT] /' "$TMP_OUT"
exit 1
