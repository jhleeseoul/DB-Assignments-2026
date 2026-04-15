#!/usr/bin/env bash
set -u

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_ACTIVATE="$BASE_DIR/.venv/bin/activate"
RUNNER="$BASE_DIR/run.py"
DB_FILE="$BASE_DIR/prj1_2.lmdb"
LOCK_FILE="${DB_FILE}-lock"

if [[ ! -f "$VENV_ACTIVATE" ]]; then
  echo "[ERR] virtualenv not found: $VENV_ACTIVATE"
  exit 1
fi

if [[ ! -f "$RUNNER" ]]; then
  echo "[ERR] runner not found: $RUNNER"
  exit 1
fi

rm -f "$DB_FILE" "$LOCK_FILE"

TMP_IN="$(mktemp)"
TMP_OUT="$(mktemp)"
trap 'rm -f "$TMP_IN" "$TMP_OUT"' EXIT

cat > "$TMP_IN" <<'SQL'
select * from ;
create table msg_ok (id int, name char(5), primary key(id));
create table dup_col (a int, a int);
create table dup_pk (a int, primary key(a), primary key(a));
create table rt_parent (id int, primary key(id));
create table rt_child (code char(5), fk int, foreign key(code) references rt_parent(id));
create table rnp_parent (id int, data int);
create table rnp_child (id int, pid int, foreign key(pid) references rnp_parent(data));
create table rex_child (id int, fk int, foreign key(fk) references no_table(id));
create table pk_missing (id int, primary key(nope));
create table fk_parent (id int, primary key(id));
create table fk_missing_col (id int, foreign key(no_col) references fk_parent(id));
create table bad_char (name char(0));
create table msg_ok (id int);
drop table no_such;
insert into msg_ok values(1,'abcde');
rename table absent to renamed;
truncate table absent;
select * from absent;
rename table msg_ok to msg_renamed;
create table occupied (id int);
rename table msg_renamed to occupied;
create table tr_parent (id int, primary key(id));
create table tr_child (id int, pid int, primary key(id), foreign key(pid) references tr_parent(id));
truncate table tr_parent;
truncate table msg_renamed;
create table drop_parent (id int, primary key(id));
create table drop_child (id int, pid int, primary key(id), foreign key(pid) references drop_parent(id));
drop table drop_parent;
drop table drop_child;
drop table drop_parent;
exit;
SQL

(
  source "$VENV_ACTIVATE"
  python "$RUNNER"
) < "$TMP_IN" > "$TMP_OUT" 2>&1

EXPECTED=(
  "SyntaxError|Syntax error"
  "CreateTableSuccess|'msg_ok' table is created"
  "DuplicateColumnDefError|Create table has failed: column definition is duplicated"
  "DuplicatePrimaryKeyDefError|Create table has failed: primary key definition is duplicated"
  "ReferenceTypeError|Create table has failed: foreign key references wrong type"
  "ReferenceNonPrimaryKeyError|Create table has failed: foreign key references non primary key column"
  "ReferenceExistenceError|Create table has failed: foreign key references non existing table or column"
  "PrimaryKeyColumnDefError|Create table has failed:cannot define non-existing column 'nope' as primary key"
  "ForeignKeyColumnDefError|Create table has failed: cannot define non-existing column 'no_col' as foreign key"
  "TableExistenceError|Create table has failed: table with the same name already exists"
  "CharLengthError|Char length should be over 0"
  "DropNoSuchTable|Drop table has failed: no such table"
  "InsertResult|The row is inserted"
  "RenameNoSuchTable|Rename table has failed: no such table"
  "TruncateNoSuchTable|Truncate table has failed: no such table"
  "SelectNoSuchTable|Select has failed: 'absent' does not exist"
  "RenameSuccess|'msg_renamed' is renamed"
  "RenameAlreadyExistError|Rename table has failed: there is already a table named 'occupied'"
  "TruncateReferencedTableError|Truncate table has failed: 'tr_parent' is referenced by another table"
  "TruncateSuccess|'msg_renamed' is truncated"
  "DropReferencedTableError|Drop table has failed: 'drop_parent' is referenced by another table"
  "DropSuccess|'drop_parent' table is dropped"
)

FAILED=0
PASS=0

for item in "${EXPECTED[@]}"; do
  name="${item%%|*}"
  pattern="${item#*|}"
  count="$(grep -F -c -- "$pattern" "$TMP_OUT" || true)"
  if [[ "$count" -ge 1 ]]; then
    echo "[OK] $name"
    PASS=$((PASS + 1))
  else
    echo "[MISS] $name => $pattern"
    FAILED=$((FAILED + 1))
  fi
done

echo
if [[ "$FAILED" -eq 0 ]]; then
  echo "[RESULT] PASS: $PASS/${#EXPECTED[@]}"
  exit 0
else
  echo "[RESULT] FAIL: $FAILED/${#EXPECTED[@]}"
  echo "--- runner output (for debug) ---"
  sed 's/^/[OUT] /' "$TMP_OUT"
  exit 1
fi
