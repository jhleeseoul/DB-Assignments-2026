# Project 1-2 시나리오 테스트셋

과제 1-2 채점 전, 수동 검증용으로 사용할 테스트 시나리오입니다.
아래 입력은 `python run.py`에 그대로 입력하거나 스크립트로 실행할 수 있습니다.

모든 예시는 실행 전 DB 초기화가 필요할 수 있습니다.
```bash
source Assignment1/.venv/bin/activate
rm -f Assignment1/prj1_2.lmdb Assignment1/prj1_2.lmdb-lock
python Assignment1/run.py
```
프롬프트는 `DB_2022-18758> ` 형식을 가정합니다.  
`(expected)` 항목은 핵심 메시지 문자열만 기재했습니다.

## SC-01 기본 DDL/DML 플로우
```text
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
```
**expected**
- `'account' table is created`
- `The row is inserted` (2회)
- `1`~`N rows in set` 형태 라인 (show tables 결과는 테이블 존재 시 개수)
- `ACCOUNT_NUMBER | BRANCH_NAME` 헤더와 두 행 출력
- `------------------------------------------------------------` 사이에 explain 출력 + `key` 컬럼이 `PRI`/빈칸 정확히 표시
- `'all_accounts' is renamed`
- `2 rows in set`
- `'all_accounts' is truncated`
- `0 rows in set`
- `'all_accounts' table is dropped`

## SC-02 Create Table 오류 검증
```text
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
```
**expected**
- `Create table has failed: column definition is duplicated`
- `Create table has failed:cannot define non-existing column 'no_col' as primary key`
- `Create table has failed: foreign key references non existing table or column`
- `Char length should be over 0`
- `'parent' table is created` (정상 생성)
- `Create table has failed: foreign key references wrong type` 또는
  `Create table has failed: foreign key references non primary key column` (테스트 환경에 맞는 메시지 확인)

## SC-03 Select / Drop / Truncate 에러 케이스
```text
select * from not_exist;
drop table not_exist;
create table parent (id int, primary key(id));
create table child (id int, parent_id int, primary key(id), foreign key(parent_id) references parent(id));
insert into parent values(1);
insert into child values(1,1);
drop table parent;
truncate table parent;
truncate table child;
drop table child;
drop table parent;
exit;
```
**expected**
- `Select has failed: 'not_exist' does not exist`
- `Drop table has failed: no such table`
- `'parent' table is created`
- `'child' table is created`
- `The row is inserted` (2회)
- `Drop table has failed: 'parent' is referenced by another table`
- `Truncate table has failed: 'parent' is referenced by another table`
- `'child' table is dropped`
- `'parent' table is dropped`

## SC-04 Rename 및 대소문자 무시
```text
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
```
**expected**
- `'mytable' table is created` (저장 시 소문자화)
- `'my_table' is renamed`
- show tables에 `my_table` 출력
- `The row is inserted`
- `A | B` 또는 `A`/`B` 대문자 헤더 출력
- `'my_table' table is dropped`

## SC-05 SELECT/INSERT 시퀀스 혼합
```text
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
```
**expected**
- `'mix' table is created`
- `The row is inserted` (3회)
- 첫 번째 select에서 1행 출력
- 두 번째 select에서 2행 출력(없던 컬럼은 `null`)
- show tables에서 `mix` 1개 존재

---

## 실행 팁
- 각 시나리오를 개별로 실행하면 영속 상태 간섭을 줄일 수 있습니다.
- 여러 시나리오를 한번에 돌릴 경우, 마지막에 `drop table` 정리를 해두면 다음 실행이 깨끗합니다.
