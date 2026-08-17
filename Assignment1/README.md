# SQL-like DBMS

Python으로 SQL 처리 과정을 직접 구현한 학습용 DBMS입니다. 데이터베이스 수업의 DDL,DML 과제를 기반으로 하며, 이후 모듈 구조, UPDATE, 추가 집계 함수, 복합 제약조건 검사, 논리·물리 실행 계획, predicate pushdown을 보강했습니다.

SQL 문장이 파싱되고 실행 계획으로 변환되어 저장소에 접근하기까지의 흐름을 학습하기 위해 제작했습니다. 테이블 정의를 담은 카탈로그와 행은 LMDB에 저장되므로 프로그램을 종료한 뒤에도 유지됩니다.

## 주요 기능

### DDL과 메타데이터

- `CREATE TABLE`, `DROP TABLE`, `RENAME TABLE`, `TRUNCATE TABLE`
- `SHOW TABLES`
- `EXPLAIN`, `DESCRIBE`, `DESC`를 통한 테이블 스키마 조회
- `INT`, `CHAR(n)`, `DATE` 타입과 `NULL` 값 처리
- 단일·복합 Primary Key와 Foreign Key 정의

### DML과 조회

- 전체 컬럼 또는 컬럼 목록을 지정하는 `INSERT`
- 선택 조건이 있는 `DELETE`
- 여러 컬럼을 한 번에 변경할 수 있는 `UPDATE`
- `AND`, `OR`, `NOT`, 괄호와 `IS [NOT] NULL`을 포함한 `WHERE`
- 복수 테이블의 inner equi-join
- 다중 컬럼 `ORDER BY`, `GROUP BY`
- `LIMIT`, `OFFSET`
- `MAX`, `MIN`, `SUM`, `COUNT`, `AVG`
- `AS`를 사용한 테이블·컬럼 alias
- 대소문자를 구분하지 않는 식별자

### 데이터 정합성

- `NOT NULL`과 데이터 타입 검사
- `CHAR(n)` 길이를 넘는 문자열의 자동 절단
- 실제 달력에 존재하는 `DATE` 값 검사
- 단일·복합 Primary Key 중복 방지
- `INSERT`와 `UPDATE` 시 Foreign Key 참조 검사
- 참조 중인 행의 `DELETE`·키 변경 방지
- 참조되는 테이블의 `DROP`·`TRUNCATE` 방지

## 지원 SQL 범위

이 프로젝트는 표준 SQL 전체가 아닌 다음 범위의 문법을 처리합니다.

| 구분 | 지원 형태와 제약 |
|---|---|
| 테이블 정의 | `INT`, `CHAR(n)`, `DATE`, `NOT NULL`, 단일·복합 PK/FK |
| 메타데이터 | `EXPLAIN table_name;`, `DESCRIBE table_name;`, `DESC table_name;`은 모두 스키마를 출력하며 쿼리 실행 계획을 출력하지 않음 |
| 데이터 변경 | 한 문장에 한 행을 넣는 `INSERT`, literal을 대입하는 다중 컬럼 `UPDATE`, 선택적 `WHERE`가 있는 `UPDATE`·`DELETE` |
| 조회와 alias | `SELECT *` 또는 컬럼·집계 함수 조회, alias를 사용할 때는 `AS` 필수 |
| JOIN | `JOIN table ON table1.column = table2.column` 형태의 inner equi-join만 지원하며 `INNER` 키워드와 outer/non-equi join은 미지원 |
| WHERE | 같은 타입끼리 비교하며 `CHAR`는 `=`·`!=`, `INT`·`DATE`는 대소 비교도 지원. NULL은 `IS NULL`·`IS NOT NULL`로 판정 |
| 집계와 정렬 | 다중 컬럼 `GROUP BY`·`ORDER BY`, `MAX`·`MIN`·`SUM`·`COUNT`·`AVG`; `ORDER BY`에는 `ASC` 또는 `DESC` 필수 |
| 행 범위 | `OFFSET` 단독 사용 가능. `LIMIT`와 함께 사용할 때는 `LIMIT` 다음에 `OFFSET`을 작성하며 두 값은 0 이상의 정수 |
| 공통 | 모든 문장은 세미콜론으로 끝나야 함 |

문법상 여러 `JOIN` 절을 연결할 수 있으며, 현재 회귀 테스트는 최대 세 테이블의 조인을 검증합니다.

## 실행 구조

`SELECT` 문은 다음 파이프라인을 따라 처리됩니다.

```text
SQL 문장
    -> Lark 파싱 / 구조화된 명령 객체 변환
    -> 논리 실행 계획 생성
    -> Predicate pushdown
    -> 물리 연산자 트리 생성
    -> 연산자 실행
    -> LMDB 카탈로그 / 행 접근
    -> 결과 포맷팅
```

물리 실행 계획은 `TableScan`, `Filter`, `NestedLoopJoin`, `GroupAggregate`, `Sort`, `LimitOffset`, `Project` 등의 연산자로 구성됩니다. `SELECT`는 하나의 LMDB read transaction에서 실행되며, 각 DDL·DML 문장은 독립된 write transaction 안에서 원자적으로 처리됩니다. 여러 SQL 문장을 사용자가 하나의 transaction으로 묶는 기능은 제공하지 않습니다.

### Predicate Pushdown

`WHERE` 절에서 `AND`로 연결된 조건을 분리한 뒤, 하나의 테이블만 참조하는 단순 비교와 NULL 조건을 해당 `TableScan`으로 내립니다. 조인 전에 불필요한 행을 제거하므로 이후 연산자가 처리하는 중간 결과와 nested-loop join의 비교 횟수를 줄일 수 있습니다.

여러 테이블을 함께 참조하는 조건과 `OR`·`NOT` 조건은 join 이후의 `Filter`에 남깁니다. 아직 secondary index가 없으므로 pushdown된 조건도 저장소의 모든 행을 읽지만, 다음 연산으로 전달되는 행은 감소합니다.

## 저장 구조

LMDB 환경 안에서 카탈로그와 행을 별도의 이름 있는 데이터베이스(named database)로 관리합니다.

- 카탈로그에는 테이블 목록, 컬럼 정의, PK/FK, 참조 관계와 행 개수를 저장합니다.
- 행은 테이블 이름과 증가하는 row ID로 구성한 key에 JSON 배열 형태로 저장합니다.
- SQL 계층은 LMDB 접근을 `storage.py`로 모아 parser와 operator가 저장 형식을 직접 다루지 않도록 분리했습니다.

## 핵심 모듈

| 구성 요소 | 역할 |
|---|---|
| [`grammar.lark`](./grammar.lark), [`parser.py`](./dbms/parser.py) | SQL 문법 정의와 parse tree의 명령 객체 변환 |
| [`plans.py`](./dbms/plans.py), [`planner.py`](./dbms/planner.py) | 논리·물리 실행 계획 구성과 predicate 배치 |
| [`operators.py`](./dbms/operators.py), [`expressions.py`](./dbms/expressions.py) | 관계 연산자 실행, 컬럼 해석과 조건식 평가 |
| [`executor.py`](./dbms/executor.py), [`commands.py`](./dbms/commands.py) | DML 실행과 명령별 처리 흐름 제어 |
| [`storage.py`](./dbms/storage.py), [`constraints.py`](./dbms/constraints.py) | LMDB 접근, 카탈로그 관리와 데이터 무결성 검사 |
| [`messages.py`](./dbms/messages.py), [`formatting.py`](./dbms/formatting.py) | 오류·결과 메시지와 result set 출력 형식 관리 |
| [`repl.py`](./dbms/repl.py), [`run.py`](./run.py) | 문장 분리, 오류 처리와 대화형 실행 진입점 |

## 시작하기

코드는 Python 3.10~3.12를 대상으로 하며, 현재 Linux(WSL2)의 Python 3.12.3 환경에서 검증했습니다. 아래 설치 명령과 shell 검증 스크립트는 Linux와 macOS의 Bash 환경을 기준으로 합니다.

```bash
cd Assignment1
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

Windows PowerShell에서는 `.venv\Scripts\Activate.ps1`로 가상환경을 활성화할 수 있으며, shell 검증 스크립트는 WSL 또는 Git Bash가 필요합니다.

여러 줄 입력과 한 줄의 여러 문장을 모두 사용할 수 있으며 `exit;`로 종료합니다. 데이터 파일은 최초 실행 시 `DB/myDB.mdb`에 생성됩니다. 기본 프롬프트의 학번과 DB 경로는 [`dbms/config.py`](./dbms/config.py)의 `STUDENT_ID`, `DB_FILE`에서 설정합니다.

## 사용 예제

```sql
create table departments (
    id int not null,
    name char(20),
    primary key (id)
);

create table students (
    id char(10) not null,
    name char(20),
    department_id int,
    primary key (id),
    foreign key (department_id) references departments (id)
);

insert into departments values (1, 'Database');
insert into students values ('2026-0001', 'Kim', 1);
insert into students values ('2026-0002', 'Lee', 1);

select d.name as department, count(s.id) as student_count
from departments as d
join students as s on d.id = s.department_id
group by d.name
order by d.name asc;

update students
set name = 'Park', department_id = 1
where id = '2026-0002';

exit;
```

프롬프트와 구분선을 제외하면 다음 결과를 확인할 수 있습니다.

```text
'departments' table is created
'students' table is created
1 row inserted
1 row inserted
1 row inserted
department | student_count
Database | 2
1 row in set
'1' row(s) updated
```

## 검증

주의: 아래 검증 명령은 테스트 격리를 위해 `DB/myDB.mdb`와 lock 파일을 삭제하고 다시 생성합니다. 보존해야 할 데이터가 있다면 실행 전에 별도로 백업해야 합니다.

전체 검증은 다음 명령 하나로 실행합니다.

```bash
bash run_scenarios.sh
```

`run_scenarios.sh`는 `validate.py`, shell 기반 실행 시나리오, 메시지 검증을 차례로 실행합니다. 일부만 확인하려면 다음 명령을 사용할 수 있습니다.

| 명령 | 검증 범위 |
|---|---|
| `python validate.py` | 문법의 정상·오류 입력, 실행 계획 형태, predicate pushdown 통계, DDL/DML·무결성 시나리오 |
| `bash verify_messages_definition.sh` | 과제에 정의된 오류·결과 메시지 형식 |

## 학습 가이드

이 프로젝트는 다음 순서로 살펴보면 SQL 처리 계층을 단계적으로 이해할 수 있습니다.

1. `grammar.lark`와 `parser.py`에서 SQL이 parse tree와 구조화된 명령으로 바뀌는 과정을 확인합니다.
2. `storage.py`와 `constraints.py`에서 스키마 메타데이터, 행과 PK/FK가 어떻게 저장되고 검사되는지 추적합니다.
3. `plans.py`와 `planner.py`에서 논리 실행 계획과 실행 가능한 물리 계획의 차이를 살펴봅니다.
4. `operators.py`에서 scan, filter, join, aggregate, sort가 입력 행을 어떻게 변환하는지 따라갑니다.
5. `validate.py`의 plan shape와 execution statistics 테스트로 predicate pushdown 전후의 중간 행과 join 비교 횟수를 관찰합니다.

이를 통해 문법 설계와 파싱, 컬럼 해석, 관계 연산자, 실행 계획, 무결성 제약, 영속 저장소와 회귀 테스트가 하나의 DBMS 안에서 어떻게 연결되는지 학습할 수 있습니다.

## 한계와 향후 작업

현재 구현은 제한된 SQL 문법을 대상으로 하며 subquery, outer join, 사용자 transaction, 동시성 제어와 crash recovery를 지원하지 않습니다. 조회는 full scan, 조인은 SQL에 작성된 순서의 nested-loop 방식입니다. LMDB를 backend로 사용하므로 page, buffer pool, WAL을 직접 관리하는 독립적인 storage engine은 아닙니다.
