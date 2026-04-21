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

TOTAL=0
PASSED=0
FAILED=0

run_case() {
  local name="$1"
  local -n expected_lines="$2"
  local input="$3"

  ((TOTAL += 1))
  echo "========================================"
  echo "Scenario: $name"
  echo "----------------------------------------"

  rm -f "$DB_FILE" "$LOCK_FILE"
  local tmp_in
  local tmp_out
  tmp_in="$(mktemp)"
  tmp_out="$(mktemp)"

  printf '%s\n' "$input" > "$tmp_in"
  (
    source "$VENV_ACTIVATE"
    python "$RUNNER" < "$tmp_in"
  ) > "$tmp_out" 2>&1
  status=$?

  if [[ $status -ne 0 ]]; then
    echo "[FAIL] runner exited with non-zero status: $status"
    FAILED=$((FAILED + 1))
    echo "[OUT]"
    sed 's/^/[  ] /' "$tmp_out"
    rm -f "$tmp_in" "$tmp_out"
    return
  fi

  local fail=0
  for expect in "${expected_lines[@]}"; do
    local pattern="$expect"
    local expected_count=1
    if [[ "$expect" == *"::"* ]]; then
      pattern="${expect%%::*}"
      expected_count="${expect##*::}"
    fi

    local actual_count
    actual_count="$(grep -F -c -- "$pattern" "$tmp_out" || true)"
    if [[ "$actual_count" -lt "$expected_count" ]]; then
      echo "[MISS] expected at least $expected_count x: $pattern"
      echo "[HAVE]  $actual_count"
      fail=1
    else
      echo "[OK] $pattern x${actual_count}"
    fi
  done

  if [[ $fail -ne 0 ]]; then
    echo "[FAIL] $name"
    sed 's/^/[OUT] /' "$tmp_out"
    FAILED=$((FAILED + 1))
  else
    echo "[PASS] $name"
    PASSED=$((PASSED + 1))
  fi

  rm -f "$tmp_in" "$tmp_out"
}

SC01_EXPECTED=(
  "'account' table is created"
  "The row is inserted::2"
  "'all_accounts' is renamed"
  "'all_accounts' is truncated"
  "'all_accounts' table is dropped"
)
SC01_INPUT="$(cat <<'SQL'
create table account (
    account_number int not null,
    branch_name char(15)
);
insert into account values(9732, 'Perryridge');
insert into account (branch_name, account_number) values('Round Hill', 305);
show tables;
select * from account;
explain account;
rename table account to all_accounts;
select * from all_accounts;
truncate table all_accounts;
select * from all_accounts;
drop table all_accounts;
exit;
SQL
)"

SC02_EXPECTED=(
  "Create table has failed: column definition is duplicated"
  "Create table has failed:cannot define non-existing column 'no_col' as primary key"
  "Create table has failed: foreign key references non existing table or column"
  "Char length should be over 0"
  "'parent' table is created"
)
SC02_INPUT="$(cat <<'SQL'
create table dupcol (
    a int,
    a int
);
create table no_pk (
    id int,
    primary key(no_col)
);
create table child (
    c_id int,
    p_id int,
    foreign key(p_id) references parent(p_id)
);
create table lenerr (
    name char(0)
);
create table parent (
    id int,
    primary key(id)
);
create table parent2 (
    id int,
    branch char(5),
    foreign key(branch) references parent(id)
);
create table parent3 (
    id int,
    branch char(5),
    foreign key(branch) references parent(branch_name)
);
exit;
SQL
)"

SC03_EXPECTED=(
  "Select has failed: 'not_exist' does not exist"
  "Drop table has failed: no such table"
  "'parent' table is created"
  "'child' table is created"
  "The row is inserted::2"
  "Drop table has failed: 'parent' is referenced by another table"
  "Truncate table has failed: 'parent' is referenced by another table"
  "'child' table is dropped"
  "'parent' table is dropped"
)
SC03_INPUT="$(cat <<'SQL'
select * from not_exist;
drop table not_exist;
create table parent (id int, primary key(id));
create table child (id int, parent_id int, primary key(id), foreign key(parent_id) references parent(id));
insert into parent values(1);
insert into child values(1,1);
drop table parent;
truncate table parent;
drop table child;
drop table parent;
exit;
SQL
)"

SC04_EXPECTED=(
  "'mytable' table is created"
  "'my_table' is renamed"
  "The row is inserted"
  "'my_table' table is dropped"
)
SC04_INPUT="$(cat <<'SQL'
create table MyTable (
    A int,
    B char(10)
);
rename table MyTable to my_table;
show tables;
insert into MY_TABLE values(1, 'abcde');
select * from my_table;
drop table my_table;
exit;
SQL
)"

SC05_EXPECTED=(
  "'mix' table is created"
  "The row is inserted::3"
)
SC05_INPUT="$(cat <<'SQL'
create table mix (
    id int,
    note char(8),
    amount int
);
insert into mix values(1, 'first', 10);
insert into mix(note, amount, id) values('second', 20, 2);
select * from mix;
insert into mix(id) values(3);
select * from mix;
show tables;
exit;
SQL
)"

SC06_EXPECTED=(
  "'bulk' table is created"
  "The row is inserted::5"
  "1 | alice | 10"
  "2 | bob | 20"
  "3 | cara | 30"
  "4 | null | null"
  "4 rows in set"
  "'bulk' is truncated"
  "0 rows in set"
  "5 | ed | 50"
  "1 row in set"
  "'bulk' table is dropped"
)
SC06_INPUT="$(cat <<'SQL'
create table bulk (
    id int,
    name char(6),
    score int
);
insert into bulk values(1, 'alice', 10);
insert into bulk values(2, 'bob', 20);
insert into bulk (id, name, score) values(3, 'cara', 30);
insert into bulk(id) values(4);
select * from bulk;
truncate table bulk;
select * from bulk;
insert into bulk values(5, 'ed', 50);
  select * from bulk;
drop table bulk;
exit;
SQL
)"

SC07_EXPECTED=(
  "'bulk_seq' table is created"
  "The row is inserted::4"
  "3 rows in set"
  "3 | c | null"
  "'bulk_seq' is truncated"
  "0 rows in set"
  "4 | x | 40"
  "1 row in set"
  "'bulk_seq' table is dropped"
)
SC07_INPUT="create table bulk_seq (id int, name char(6), score int); insert into bulk_seq values(1, 'a', 10); insert into bulk_seq values(2, 'b', 20); insert into bulk_seq (id, name) values(3, 'c'); select * from bulk_seq; truncate table bulk_seq; select * from bulk_seq; insert into bulk_seq values(4, 'x', 40); select * from bulk_seq; drop table bulk_seq; exit;"

run_case "SC-01 기본 DDL/DML 플로우" SC01_EXPECTED "$SC01_INPUT"
run_case "SC-02 Create Table 오류 검증" SC02_EXPECTED "$SC02_INPUT"
run_case "SC-03 SELECT/DROP/TRUNCATE 오류 검증" SC03_EXPECTED "$SC03_INPUT"
run_case "SC-04 Rename 및 대소문자 무시" SC04_EXPECTED "$SC04_INPUT"
run_case "SC-05 SELECT/INSERT 시퀀스 혼합" SC05_EXPECTED "$SC05_INPUT"
run_case "SC-06 멀티로우 조회/INSERT/DELETE 경로" SC06_EXPECTED "$SC06_INPUT"
run_case "SC-07 한 줄 query sequence 멀티로우 경로" SC07_EXPECTED "$SC07_INPUT"

echo "========================================"
echo "Summary: PASS=$PASSED, FAIL=$FAILED, TOTAL=$TOTAL"
if [[ $FAILED -ne 0 ]]; then
  exit 1
fi
