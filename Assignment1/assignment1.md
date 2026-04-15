# Project 1-1: SQL Parser

2026 Spring Database 과제인 **Project 1-1(SQL Parser)** 를 위한 README입니다. 이 문서는 과제의 목표, 구현 범위, 실행 방법, 입력/출력 규칙, 지원해야 하는 SQL 구문, 제출물 및 채점 유의사항을 한 번에 확인할 수 있도록 정리한 문서입니다.

---

## 1. 프로젝트 개요

프로젝트 1은 SQL의 기본 기능을 수행할 수 있는 간단한 DBMS를 구현하는 장기 프로젝트이며, 총 3단계로 구성됩니다.

- **Project 1-1**: SQL Parser 구현
- **Project 1-2**: DDL 구현
- **Project 1-3**: DML 구현

이번 단계인 **Project 1-1**의 목표는 다음과 같습니다.

- `grammar.lark` 파일을 완성하여 SQL 문법을 정의한다.
- `run.py` 파일을 작성하여 사용자 입력을 받아 SQL 파싱을 수행한다.
- 파싱 결과를 바탕으로 어떤 명령이 요청되었는지 지정된 형식으로 출력한다.

즉, 이번 과제는 실제 DB 동작을 구현하는 것이 아니라, **입력된 SQL 구문을 파싱하여 문법적으로 올바른지 판별하고 어떤 명령인지 인식하는 SQL 파서**를 만드는 것이 핵심입니다.

---

## 2. SQL Parser란?

SQL 파서는 사용자가 입력한 SQL 구문을 분석하여, 해당 구문이 정의된 문법에 맞는 올바른 문장인지 판단하는 도구입니다.

파싱(parsing)은 다음을 포함합니다.

- 입력 구문이 문법 구조를 따르는지 확인
- 구문이 완전한지 검사
- 어떤 종류의 명령인지 해석 가능하도록 분석

과제에서 구현할 프로그램은 사용자로부터 SQL 구문을 입력받고, 이를 **반드시 Lark의 `parse()` API를 통해 파싱한 뒤**, 요청된 명령 종류를 아래처럼 출력해야 합니다.

### 예시

```text
DB_2026-12345> select ID from student;
DB_2026-12345> 'SELECT' requested
DB_2026-12345>
```

---

## 3. 개발 환경

다음 환경을 사용해야 합니다.

- **Python 3.10 ~ 3.12**
- **Lark API**

### 설치 예시

```bash
pip install lark
```

참고:
- pip 설치 가이드
- Lark 공식 문서

---

## 4. 구현 파일

과제에서 핵심적으로 작성해야 하는 파일은 다음 두 개입니다.

### 4.1 `grammar.lark`

- Lark가 해석할 수 있도록 **EBNF 형식**으로 SQL 문법을 정의하는 파일
- 제공된 `grammar_skeleton.lark`를 자유롭게 수정하여 사용 가능
- 타입, 연산자 등 일부 기초 요소는 스켈레톤에 포함될 수 있으나, 과제 범위의 문법은 직접 완성해야 함
- 특히 문서와 Q&A에서 언급된 `drop`, `desc`, `show`, `delete`, `limit clause`, `offset clause` 등도 직접 작성해야 함

### 4.2 `run.py`

- `grammar.lark`를 읽어 Lark parser를 생성하는 실행 파일
- 사용자 입력을 받아 SQL query를 누적해서 읽고 파싱해야 함
- 파싱 결과에 따라 지정된 메시지를 출력해야 함
- **반드시 주석이 포함되어야 함**
- `python3 run.py` 실행만으로 SQL parser가 정상 동작해야 함

필요하다면 여러 파일로 분할 가능하지만, 이 경우 **리포트에 분할 내용을 명시해야 하며**, 최종적으로는 반드시 `python3 run.py`만으로 동작해야 합니다.

---

## 5. Lark와 EBNF

### 5.1 Lark

Lark는 Python용 파싱 라이브러리입니다.

- `.lark` 파일에 작성한 grammar를 기준으로 parser 생성 가능
- parser는 입력된 SQL 구문을 파싱해 결과를 Tree 형태로 생성
- `pretty()` 등을 통해 파싱 결과를 확인 가능

예시:

```python
with open('grammar.lark') as file:
    sql_parser = Lark(file.read(), start="command", lexer="basic")

output = sql_parser.parse('select ID from student;')
```

### 5.2 EBNF

문법은 EBNF(Extended Backus-Naur Form) 형식으로 작성해야 합니다.

기본 개념:

- `<symbol> ::= <expression>`
- `|` : 여러 표현식 중 하나를 의미
- `[]` : 생략 가능한 요소를 의미

예시:

```text
<digit> ::= "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9"
<number> ::= <digit> | <number> <digit>
```

---

## 6. 필수 요구사항

아래 조건을 만족하도록 `grammar.lark`와 `run.py`를 구현해야 합니다.

### 6.1 기본 요구사항

- 2.1절에 명시된 **모든 SQL 구문**을 파싱할 수 있어야 함
- 2.2절에 명시된 **SQL query sequence**를 파싱할 수 있어야 함
- 하나의 SQL 구문이 **여러 줄에 걸쳐 입력되어도** 파싱할 수 있어야 함
- 입력 받은 구문은 **반드시 Lark의 `parse()` 함수**를 거쳐야 함
- 제공된 `grammar_skeleton.lark`는 자유롭게 수정 가능

### 6.2 프롬프트 규칙

프로그램은 실행 직후부터 사용자 입력을 받을 준비가 되었음을 표시하는 프롬프트를 출력해야 합니다.

- 프롬프트 형식: `DB_학번> `
- 예시: `DB_2026-12345> `

또한,

- **모든 출력 앞에도 동일한 프롬프트를 붙여야 함**
- 자동 채점과 직접 관련되므로, `run.py` 실행 시 **맨 처음 출력도 반드시 프롬프트여야 함**

예시:

```text
DB_2026-12345> show tables;
DB_2026-12345> 'SHOW TABLES' requested
DB_2026-12345>
```

### 6.3 에러 처리

2.1절 및 2.2절에 명시된 SQL 구문 형태가 아닌 입력은 다음과 같이 처리해야 합니다.

```text
DB_2026-12345> Syntax error
```

추가 규칙:

- 하나의 SQL 구문이 여러 줄로 입력되는 경우, **첫 번째 줄에만 프롬프트를 표시**해야 함
- 모든 입력은 **소문자만 들어오는 경우만 평가**함
- 출력은 **대소문자를 구별하지 않고 평가**함

### 6.4 식별자 제한

사용자가 정의하는 식별자(예: 테이블 이름, 컬럼 이름 등)로 **SQL 예약어를 사용할 수 없어야 함**.

- 예약어를 이름으로 사용하면 `Syntax error` 출력
- 예: `select`, `insert`, `update`, `delete`, `from`, `where` 등 프로젝트 범위의 예약어

---

## 7. 입력 처리 규칙

### 7.1 여러 줄 입력

하나의 SQL 문장이 여러 줄로 입력될 수 있습니다.

- 개행 문자(`\r`, `\n` 등)가 입력 중간에 있어도 입력 종료로 간주하면 안 됨
- SQL 문장의 끝은 세미콜론(`;`)으로 판단
- 요구사항 문서 기준, 평가에서는 **구문의 끝에 항상 세미콜론이 존재한다고 가정**함

예를 들어 아래와 같은 입력도 정상 처리되어야 합니다.

```text
DB_2026-12345> select account_number, branch, deposit
from account
where deposit >= 10000
limit 1
offset 1;
DB_2026-12345> 'SELECT' requested
DB_2026-12345>
```

### 7.2 세미콜론 처리 관련 주의

Q&A 자료에서는 다음이 추가로 언급됩니다.

- 세미콜론 전후 whitespace는 무시해야 함
- `show tables ;` 같은 형태도 정상 동작해야 함
- semicolon이 올 때까지 input을 계속 받아야 함

### 7.3 Query Sequence

여러 개의 query가 세미콜론으로 구분되어 한 줄에 들어오는 경우, **순차적으로 처리**해야 합니다.

형식:

```text
query1;query2;query3;...;queryN;
```

규칙:

- `query1`부터 순서대로 처리
- 만약 `queryK`에서 에러가 발생하더라도, `query1 ~ queryK-1`는 정상 처리되어야 함
- Query Sequence 평가는 **개행 없는 한 줄 입력**만 대상으로 함

예시:

```text
DB_2026-12345> insert into account values(9732, 'Perryridge'); show tables; insert into account; desc account;
DB_2026-12345> 'INSERT' requested
DB_2026-12345> 'SHOW TABLES' requested
DB_2026-12345> Syntax error
DB_2026-12345>
```

### 7.4 `exit` 처리

- `exit;`가 입력되면 즉시 종료해야 함
- 아무 메시지도 추가 출력하지 않고 종료해야 함
- 여러 입력 중간에 `exit`가 있으면 **그 전까지 처리한 후 종료**해야 함

즉, 예시의 `database@server:~$`는 셸 프롬프트 예시일 뿐, 프로그램이 직접 출력하는 것이 아닙니다.

---

## 8. 지원해야 하는 SQL 구문

아래 모든 구문을 파싱할 수 있어야 합니다.

### 8.1 CREATE TABLE

정의:

```sql
create table table_name (
    column_name data_type [not null],
    ...
    [primary key(column_name1, column_name2, ...),]
    [foreign key(column_name3) references table_name1(column_name4),]
    [foreign key(column_name5) references table_name2(column_name6)]
    ...
);
```

조건:

- `table_name`, `column_name`은 **알파벳 및 `_`만으로 구성**

출력:

```text
'CREATE TABLE' requested
```

---

### 8.2 DROP TABLE

정의:

```sql
drop table table_name;
```

출력:

```text
'DROP TABLE' requested
```

---

### 8.3 EXPLAIN / DESCRIBE / DESC

정의:

```sql
explain table_name;
describe table_name;
desc table_name;
```

출력:

- `explain` → `'EXPLAIN' requested`
- `describe` → `'DESCRIBE' requested`
- `desc` → `'DESC' requested`

---

### 8.4 INSERT

정의:

```sql
insert into table_name [(col_name1, col_name2, ...)] values(value1, value2, ...);
```

출력:

```text
'INSERT' requested
```

주의:

- `value`는 **INT, CHAR, DATE 타입만 지원**
- `FLOAT` 등 부동소수점 타입은 고려하지 않음
- `column 수`와 `value 수`가 다른 경우는 평가 대상에서 제외

---

### 8.5 DELETE

정의:

```sql
delete from table_name [where clause];
```

출력:

```text
'DELETE' requested
```

---

### 8.6 SELECT

정의:

```sql
select [table_name.]column_name [as name], ...
from table_name [as name], ...
[where clause]
[limit clause]
[offset clause];
```

출력:

```text
'SELECT' requested
```

지원 예시:

```sql
select * from account;
```

```sql
select customer_name, borrower.loan_number, amount
from borrower, loan
where borrower.loan_number = loan.loan_number and branch_name = 'Perryridge';
```

```sql
select * from account limit 5;
```

```sql
select * from student offset 10;
```

```sql
select account_number, branch, deposit
from account
where deposit >= 10000
limit 1
offset 1;
```

---

### 8.7 SHOW TABLES

정의:

```sql
show tables;
```

출력:

```text
'SHOW TABLES' requested
```

---

### 8.8 UPDATE

정의:

```sql
update table_name set column_name = comparable_value [where clause];
```

출력:

```text
'UPDATE' requested
```

---

### 8.9 RENAME

정의:

```sql
rename table table_name to new_table_name[, table_name2 to new_table_name2] ...;
```

출력:

```text
'RENAME TABLE' requested
```

---

### 8.10 TRUNCATE

정의:

```sql
truncate table table_name;
```

출력:

```text
'TRUNCATE TABLE' requested
```

---

### 8.11 EXIT

정의:

```sql
exit;
```

동작:

- 즉시 종료
- 어떤 문장도 출력하지 않음

---

## 9. 출력 규칙 정리

각 SQL 문장이 정상 파싱되었을 경우, 아래와 같은 형식으로 출력해야 합니다.

| 입력 구문 | 출력 |
|---|---|
| `create table ...;` | `'CREATE TABLE' requested` |
| `drop table ...;` | `'DROP TABLE' requested` |
| `explain ...;` | `'EXPLAIN' requested` |
| `describe ...;` | `'DESCRIBE' requested` |
| `desc ...;` | `'DESC' requested` |
| `insert into ...;` | `'INSERT' requested` |
| `delete from ...;` | `'DELETE' requested` |
| `select ...;` | `'SELECT' requested` |
| `show tables;` | `'SHOW TABLES' requested` |
| `update ...;` | `'UPDATE' requested` |
| `rename table ...;` | `'RENAME TABLE' requested` |
| `truncate table ...;` | `'TRUNCATE TABLE' requested` |
| 문법 오류 | `Syntax error` |
| `exit;` | 아무 출력 없이 종료 |

모든 줄의 출력 앞에는 프롬프트 `DB_학번> `가 붙어야 합니다.

---

## 10. 구현 권장 구조

아래는 구현 시 권장되는 흐름입니다.

1. 프로그램 시작 시 프롬프트 출력
2. 세미콜론(`;`)이 나올 때까지 사용자 입력 누적
3. 하나의 입력 문자열 안에 query sequence가 있으면 순서대로 분리
4. 각 query를 Lark의 `parse()`로 파싱
5. 파싱 성공 시 transformer 또는 tree rule을 이용해 query 종류 판별
6. 지정된 `'... requested'` 메시지 출력
7. 파싱 실패 시 `Syntax error` 출력
8. `exit;` 처리 시 즉시 종료

문서 예시에는 `Transformer` 사용 방식이 제시되어 있습니다.

```python
class MyTransformer(Transformer):
    def create_table_query(self, items):
        # implement here
        pass

    def drop_table_query(self, items):
        # implement here
        pass

output = sql_parser.parse(query)
MyTransformer().transform(output)
```

핵심은 **parse 결과를 바탕으로 명령 종류를 판별해야 한다는 점**입니다. 단순히 첫 단어만 보고 출력하는 식의 우회 구현은 감점 또는 0점 처리 대상입니다.

---

## 11. 실행 방법

`run.py`와 `grammar.lark`가 같은 디렉터리에 있다고 가정합니다.

```bash
python3 run.py
```

실행 시 즉시 아래와 같은 프롬프트가 보여야 합니다.

```text
DB_2026-12345>
```

이후 사용자가 SQL 구문을 입력하면, 세미콜론 기준으로 문장을 완성해 파싱하고 결과를 출력해야 합니다.

---

## 12. 제출 형식

아래 3개 파일을 압축하여 ETL에 제출합니다.

압축 파일 이름 형식:

```text
PRJ1-1_학번.zip
```

예시:

```text
PRJ1-1_2026-12345.zip
```

포함 파일:

1. `grammar.lark`
2. `run.py`
3. 리포트

### 리포트 작성 내용

다음 항목 중 해당되는 내용을 작성합니다.

- Python 버전
- 외부 라이브러리 사용 시 라이브러리 정보
- 핵심 모듈과 알고리즘 설명
- 구현한 내용에 대한 간략한 설명
- 구현하지 못한 요구사항이 있다면 그 내용
- 프로젝트를 하며 느낀 점 및 기타 사항

리포트 분량:

- **1장 권장**
- **최대 2장 이내**

---

## 13. 제출 기한 및 지각 정책

- **제출 마감**: **2026/04/05 (Sun), 11:59 P.M.**
- 마감 후 **24시간 이내 제출**: **10% 감점**
- 마감 후 **24~48시간 이내 제출**: **20% 감점**
- 마감 후 **48시간 초과**: **0점 처리**

---

## 14. 채점 및 감점 유의사항

### 14.1 부정 행위

다음은 0점 처리 대상입니다.

- 다른 사람의 코드를 참조하는 행위
- 이전 수강자의 코드를 참조하는 행위
- 표절 방지 프로그램 기준 표절로 판단되는 경우

### 14.2 악의적 우회

다음 역시 0점 처리 대상입니다.

- query가 Lark parsing API를 거치지 않는 경우
- 출력만 맞추기 위해 query의 첫 번째 단어만 보고 처리하는 경우
- 부분 점수를 노리고 파싱 결과와 무관하게 `Syntax error`만 출력하는 경우

### 14.3 형식 관련 감점

다음은 감점 대상입니다.

- 문서에 명시된 출력 형식을 지키지 않은 경우
- `run.py`에 주석이 없는 경우
- `run.py` 실행 시 시작 프롬프트가 요구 형식과 다를 경우

---

## 15. Q&A에서 확인된 추가 스펙

문서 본문 외에 소개 자료의 Q&A에서 확인되는 추가 사항은 다음과 같습니다.

### 15.1 `grammar.lark`는 수정해야 하는가?

- **수정해야 함**
- PL 지식이 있으면 더 수월하겠지만, 일부 범위는 충분히 직접 구현 가능함

### 15.2 세미콜론으로 끝나지 않는 query는 에러인가?

- 소개 자료 Q&A 기준으로는 **semicolon이 들어올 때까지 input을 받아야 함**
- 또한 semicolon 앞뒤 whitespace는 무시해야 함
- 다만 프로젝트 문서의 평가 조건에서는 **구문의 끝에 항상 세미콜론이 존재한다고 가정**함

즉, 구현은 누적 입력 방식으로 하는 것이 안전합니다.

### 15.3 여러 input 중간에 `exit`가 있으면?

- `exit` 이전까지 처리 후 종료

### 15.4 Prompt 구현은 왜 중요한가?

- 자동 채점이 prompt 형식에 의존하므로 매우 중요함
- 실행 직후부터 `DB_학번> ` 형식의 prompt를 정확히 출력해야 함

---

## 16. 테스트 예시

### 16.1 정상 입력

```text
DB_2026-12345> create table account (
account_number int not null,
branch_name char(15),
primary key(account_number)
);
DB_2026-12345> 'CREATE TABLE' requested
DB_2026-12345>
```

```text
DB_2026-12345> select * from account limit 5;
DB_2026-12345> 'SELECT' requested
DB_2026-12345>
```

```text
DB_2026-12345> show tables;
DB_2026-12345> 'SHOW TABLES' requested
DB_2026-12345>
```

### 16.2 Query Sequence

```text
DB_2026-12345> insert into account values(9732, 'Perryridge'); show tables; insert into account; desc account;
DB_2026-12345> 'INSERT' requested
DB_2026-12345> 'SHOW TABLES' requested
DB_2026-12345> Syntax error
DB_2026-12345>
```

### 16.3 종료

```text
DB_2026-12345> exit;
```

위 경우 추가 문장 없이 즉시 종료해야 합니다.

---

## 17. 참고 자료

- Lark 공식 문서
- Lark Grammar Reference
- EBNF 위키 문서

---

## 18. 최종 체크리스트

제출 전 아래 항목을 확인하세요.

- [ ] `python3 run.py`로 바로 실행되는가?
- [ ] 실행 직후 프롬프트가 `DB_학번> ` 형식으로 출력되는가?
- [ ] 모든 출력 앞에 프롬프트가 붙는가?
- [ ] 여러 줄 SQL 입력을 처리하는가?
- [ ] query sequence를 순차 처리하는가?
- [ ] `exit;`를 만나면 즉시 종료하는가?
- [ ] 문법 오류 시 `Syntax error`를 출력하는가?
- [ ] 예약어를 식별자로 쓰면 에러 처리하는가?
- [ ] 모든 SQL 문장을 실제로 Lark `parse()`로 파싱하는가?
- [ ] `run.py`에 주석이 포함되어 있는가?
- [ ] 리포트에 필요한 내용을 작성했는가?

---

## 19. 비고

과제 관련 질문은 **ETL Q&A 게시판**을 이용해야 하며, 가능하면 비밀글이나 이메일 문의는 지양하라고 안내되어 있습니다. 또한 개별 코드 구현 방식에 대한 직접적인 문의는 형평성상 답변되지 않을 수 있습니다.

