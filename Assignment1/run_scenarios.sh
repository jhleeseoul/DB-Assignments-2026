#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_ACTIVATE="$BASE_DIR/.venv/bin/activate"
RUNNER="$BASE_DIR/run.py"
VALIDATOR="$BASE_DIR/validate.py"
VERIFY_MSG="$BASE_DIR/verify_messages_definition.sh"
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
if [[ ! -f "$VALIDATOR" ]]; then
  echo "[ERR] validator not found: $VALIDATOR"
  exit 1
fi
if [[ ! -f "$VERIFY_MSG" ]]; then
  echo "[ERR] message verifier not found: $VERIFY_MSG"
  exit 1
fi

source "$VENV_ACTIVATE"

TOTAL=0
PASSED=0
FAILED=0

assert_contains() {
  local file="$1"
  local name="$2"
  local pattern="$3"
  local need="${4:-1}"
  local got

  got="$(grep -F -c -- "$pattern" "$file" || true)"
  if [[ "$got" -ge "$need" ]]; then
    echo "[OK] $name => $pattern x$got"
    return 0
  fi

  echo "[MISS] $name => $pattern (need=$need, got=$got)"
  return 1
}

run_case() {
  local case_name="$1"
  local input_sql="$2"
  shift 2
  local patterns=("$@")

  TOTAL=$((TOTAL + 1))
  rm -f "$DB_FILE" "$LOCK_FILE"

  local in_file out_file
  in_file="$(mktemp)"
  out_file="$(mktemp)"

  printf '%s\n' "$input_sql" > "$in_file"

  if ! python "$RUNNER" < "$in_file" > "$out_file" 2>&1; then
    echo "[FAIL] $case_name (runner returned non-zero)"
    sed 's/^/[OUT] /' "$out_file"
    FAILED=$((FAILED + 1))
    rm -f "$in_file" "$out_file"
    return
  fi

  local failed=0
  for spec in "${patterns[@]}"; do
    local pat="$spec"
    local need=1
    if [[ "$spec" == *"::"* ]]; then
      pat="${spec%%::*}"
      need="${spec##*::}"
    fi

    if ! assert_contains "$out_file" "$case_name" "$pat" "$need"; then
      failed=1
    fi
  done

  if [[ "$failed" -eq 0 ]]; then
    echo "[PASS] $case_name"
    PASSED=$((PASSED + 1))
  else
    echo "[FAIL] $case_name"
    sed 's/^/[OUT] /' "$out_file"
    FAILED=$((FAILED + 1))
  fi

  rm -f "$in_file" "$out_file"
}

echo "[STEP] Running validate.py"
if ! python "$VALIDATOR"; then
  echo "[FAIL] validate.py failed"
  exit 1
fi

SC_A_INPUT="$(cat <<'SQL'
create table a (id int, name char(6)); insert into a values(1,'alpha'); insert into a values(2,'beta'); select * from a order by id asc; exit;
SQL
)"

SC_B_INPUT="$(cat <<'SQL'
create table split_t (id int, txt char(20));
insert into split_t values(1, 'abc;def');
insert into split_t values(2, 'x;y;z');
select * from split_t order by id asc;
exit;
SQL
)"

SC_C_INPUT="$(cat <<'SQL'
create table p (id int not null, primary key(id));
create table c (id int not null, pid int, primary key(id), foreign key(pid) references p(id));
insert into p values(1);
insert into p values(2);
insert into c values(1,1);
delete from p;
select * from p order by id asc;
exit;
SQL
)"

SC_D_INPUT="$(cat <<'SQL'
create table w1 (id int, c char(3));
create table w2 (id int);
insert into w1 values(1,'abc');
insert into w2 values(1);
select w1.id from w1 join w2 on w1.id = w2.id where id = 1;
select w1.id from w1 where c > 'a';
select * from w1 order by id asc limit -1;
select * from w1 offset 1 limit 1;
exit;
SQL
)"

SC_E_INPUT="$(cat <<'SQL'
create table j1 (id int, c char(3));
create table j2 (id int, c char(3));
insert into j1 values(1,'ab');
insert into j2 values(1,'cd');
select * from j1 join j2 on j1.id = j2.id;
select j1.id from j1 join j2 on j1.c = j2.id;
select j1.id from j1 join j2 on t9.id = j2.id;
exit;
SQL
)"

SC_F_INPUT="$(cat <<'SQL'
create table g1 (id int, grp char(1), v int, d date);
insert into g1 values(1,'A',10,2026-01-01);
insert into g1 values(2,'A',20,2026-01-03);
insert into g1 values(3,'B',null,null);
select g1.grp, max(g1.d), min(g1.d), sum(g1.v) from g1 group by g1.grp order by g1.grp asc;
select g1.grp, g1.id, sum(g1.v) from g1 group by g1.grp;
exit;
SQL
)"

SC_G_INPUT="$(cat <<'SQL'
create table MyTable (ID int, Name char(5));
insert into MYTABLE values(1, 'abcde');
select x.id as xid, x.name as xname from mytable as x where x.id = 1 order by x.id asc;
rename table mytable to my_table;
show tables;
exit;
SQL
)"

SC_H_INPUT="$(cat <<'SQL'
insert into no_table values(1);
delete from no_table;
select * from no_table;
exit;
SQL
)"

SC_I_INPUT="$(cat <<'SQL'
create table md (id int, d date, name char(2));
explain md;
desc md;
describe md;
exit;
SQL
)"

SC_J_INPUT="$(cat <<'SQL'
create table p_comp (a int, b int, primary key(a,b));
create table c_partial (x int, foreign key(x) references p_comp(a));
create table c_comp (a int, b int, foreign key(a,b) references p_comp(a,b));
insert into p_comp values(1,10);
insert into c_comp values(1,10);
delete from p_comp where a = 1 and b = 10;
select * from p_comp order by a asc;
exit;
SQL
)"

SC_K_INPUT="$(cat <<'SQL'
create table upd (id int not null, name char(4), score int);
insert into upd values(1,'ann',10);
insert into upd values(2,null,20);
update upd set name = 'longname', score = 25 where id = 2;
update upd set score = 7;
select * from upd order by id asc;
update upd set id = null where id = 1;
update upd set no_col = 1;
update upd set score = 'bad';
exit;
SQL
)"

SC_L_INPUT="$(cat <<'SQL'
create table agg2 (grp char(1), bucket int, val int, note char(3));
insert into agg2 values('A',2,10,'aa');
insert into agg2 values('A',1,20,null);
insert into agg2 values('B',2,30,'bb');
insert into agg2 values('B',1,null,'cc');
select grp, bucket, count(*), count(note), avg(val), avg(note), sum(val)
from agg2
group by grp, bucket
order by grp asc, bucket desc;
select count(*), count(note), avg(val), avg(note) from agg2;
exit;
SQL
)"

SC_M_INPUT="$(cat <<'SQL'
create table p2 (id int not null, label char(4), primary key(id));
create table c2 (id int not null, pid int, note char(4), primary key(id), foreign key(pid) references p2(id));
create table cp2 (a int not null, b int not null, primary key(a,b));
create table cc2 (id int not null, a int, b int, primary key(id), foreign key(a,b) references cp2(a,b));
insert into p2 values(1,'one');
insert into p2 values(1,'dupe');
insert into c2 values(1,9,'bad');
insert into c2 values(1,null,'free');
insert into c2 values(2,1,'ok');
update p2 set id = 2 where id = 1;
select * from p2 order by id asc;
update p2 set label = 'uno' where id = 1;
update c2 set pid = 7 where id = 2;
update c2 set pid = null where id = 2;
update c2 set id = 1 where id = 2;
select * from c2 order by id asc;
insert into cp2 values(1,10);
insert into cp2 values(1,10);
insert into cc2 values(1,1,null);
insert into cc2 values(2,1,99);
update cc2 set b = 10 where id = 1;
update cp2 set a = 2 where a = 1 and b = 10;
select * from cp2 order by a asc;
select * from cc2 order by id asc;
exit;
SQL
)"

SC_N_INPUT="$(cat <<'SQL'
create table dates2 (id int not null, d date, primary key(id));
insert into dates2 values(1,2024-02-29);
insert into dates2 values(2,2026-02-29);
update dates2 set d = 2026-04-31 where id = 1;
select * from dates2 where d = 2026-13-01;
select * from dates2 order by id asc;
exit;
SQL
)"

run_case \
  "SC-A one-line sequence" \
  "$SC_A_INPUT" \
  "'a' table is created" \
  "1 row inserted::2" \
  "1 | alpha" \
  "2 | beta"

run_case \
  "SC-B semicolon in string" \
  "$SC_B_INPUT" \
  "'split_t' table is created" \
  "1 row inserted::2" \
  "1 | abc;def" \
  "2 | x;y;z"

run_case \
  "SC-C referential delete block" \
  "$SC_C_INPUT" \
  "'2' row(s) are not deleted due to referential integrity" \
  "id" \
  "2 rows in set"

run_case \
  "SC-D where/limit error mix" \
  "$SC_D_INPUT" \
  "Where clause contains ambiguous column reference" \
  "Trying to compare incomparable columns or values" \
  "Select has failed: LIMIT/OFFSET clause should be a non-negative integer" \
  "Syntax error"

run_case \
  "SC-E join normal + errors" \
  "$SC_E_INPUT" \
  "j1.id | j1.c | j2.id | j2.c" \
  "1 | ab | 1 | cd" \
  "Trying to compare incomparable columns or values" \
  "Join clause trying to reference non existing column"

run_case \
  "SC-F group by aggregate" \
  "$SC_F_INPUT" \
  "g1.grp | max(g1.d) | min(g1.d) | sum(g1.v)" \
  "A | 2026-01-03 | 2026-01-01 | 30" \
  "B | null | null | 0" \
  "Select has failed: column 'id' must either be included in the GROUP BY clause or be used in an aggregate function"

run_case \
  "SC-G case-insensitive alias/rename" \
  "$SC_G_INPUT" \
  "'mytable' table is created" \
  "1 row inserted" \
  "xid | xname" \
  "1 | abcde" \
  "'my_table' is renamed" \
  "my_table"

run_case \
  "SC-H no-such-table trio" \
  "$SC_H_INPUT" \
  "Insert has failed: no such table" \
  "Delete has failed: no such table" \
  "Select has failed: 'no_table' does not exist"

run_case \
  "SC-I DATE metadata explain/desc/describe" \
  "$SC_I_INPUT" \
  "'md' table is created" \
  "column_name | type::3" \
  "| date::3" \
  "3 rows in set::3"

run_case \
  "SC-J composite FK validation + RI delete block" \
  "$SC_J_INPUT" \
  "'p_comp' table is created" \
  "Create table has failed: foreign key references non primary key column" \
  "'c_comp' table is created" \
  "1 row inserted::2" \
  "'1' row(s) are not deleted due to referential integrity" \
  "a | b" \
  "1 | 10" \
  "1 row in set"

run_case \
  "SC-K phase1 update behavior" \
  "$SC_K_INPUT" \
  "'upd' table is created" \
  "1 row inserted::2" \
  "'1' row(s) updated" \
  "'2' row(s) updated" \
  "1 | ann | 7" \
  "2 | long | 7" \
  "Update has failed: 'id' is not nullable" \
  "Update has failed: 'no_col' does not exist" \
  "Update has failed: types are not matched"

run_case \
  "SC-L phase1 aggregate multi group/order" \
  "$SC_L_INPUT" \
  "'agg2' table is created" \
  "1 row inserted::4" \
  "grp | bucket | count(*) | count(note) | avg(val) | avg(note) | sum(val)" \
  "A | 2 | 1 | 1 | 10.0 | 0 | 10" \
  "A | 1 | 1 | 0 | 20.0 | 0 | 20" \
  "B | 2 | 1 | 1 | 30.0 | 0 | 30" \
  "B | 1 | 1 | 1 | 0 | 0 | 0" \
  "count(*) | count(note) | avg(val) | avg(note)" \
  "4 | 3 | 20.0 | 0"

run_case \
  "SC-M phase2 pk/fk correctness" \
  "$SC_M_INPUT" \
  "'p2' table is created" \
  "'c2' table is created" \
  "'cp2' table is created" \
  "'cc2' table is created" \
  "1 row inserted::5" \
  "Insert has failed: primary key duplication::2" \
  "Insert has failed: referential integrity violation::2" \
  "Update has failed: referential integrity violation::3" \
  "Update has failed: primary key duplication" \
  "'1' row(s) updated::3" \
  "1 | one" \
  "1 | null | free" \
  "2 | null | ok" \
  "1 | 10" \
  "1 | 1 | 10"

run_case \
  "SC-N phase2 date validation" \
  "$SC_N_INPUT" \
  "'dates2' table is created" \
  "1 row inserted" \
  "Date value is invalid::3" \
  "1 | 2024-02-29" \
  "1 row in set"

echo "[STEP] Running message verifier"
if ! bash "$VERIFY_MSG"; then
  echo "[FAIL] verify_messages_definition.sh failed"
  exit 1
fi

echo "========================================"
echo "Summary: PASS=$PASSED FAIL=$FAILED TOTAL=$TOTAL"

if [[ "$FAILED" -ne 0 ]]; then
  exit 1
fi

echo "[RESULT] PASS"
