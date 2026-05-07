# Project 1-3 시나리오 테스트셋 (확장판)

이 문서는 과제 1-3 검증 범위를 넓히기 위한 시나리오 모음이다.

검증 범위:
- INSERT/DELETE/SELECT 핵심 기능
- WHERE/JOIN/ORDER BY/LIMIT/OFFSET
- Optional GROUP BY + MAX/MIN/SUM
- 메시지 정합성(README 메시지 표)
- 문장 분리(멀티라인, 문자열 내 `;`)
- 엣지 케이스(모호성, 타입 불일치, NULL, FK 차단)

## 실행 준비

```bash
source Assignment1/.venv/bin/activate
rm -f Assignment1/DB/myDB.mdb Assignment1/DB/myDB.mdb-lock
python Assignment1/run.py
```

## 시나리오 목록

### SC-01 Prerequisite + End-to-End SELECT 파이프라인
- 학생/강의/수강신청 3개 테이블 생성
- INSERT 7건
- JOIN + WHERE + ORDER BY
- GROUP BY + aggregate + LIMIT/OFFSET
- 검증 포인트:
- `char(20)` truncate
- 조인 결과/집계 결과 정합성

### SC-02 INSERT 타입/개수/컬럼/NOT NULL 오류
- 없는 테이블 insert
- 없는 컬럼 지정 insert
- 타입 불일치
- 값 개수 불일치
- 중복 컬럼 리스트
- non-nullable 컬럼 NULL
- 검증 포인트:
- `Insert has failed: ...` 메시지 분기 정확성

### SC-03 DELETE + Referential Integrity All-or-Nothing
- 부모/자식(FK) 생성
- 부모 전체 삭제 시 FK 차단
- 자식 선삭제 후 부모 삭제 성공
- 검증 포인트:
- `... not deleted due to referential integrity`
- 삭제 count 메시지

### SC-04 WHERE 정상 동작(괄호/NOT/IS NULL/IS NOT NULL)
- `(cond1 and not cond2) or cond3`
- `is null`, `is not null`
- 검증 포인트:
- boolean expression 평가
- NULL predicate 평가

### SC-05 WHERE 오류 분기
- AmbiguousReference
- TableNotSpecified
- ColumnNotExist
- IncomparableError

### SC-06 SELECT 컬럼 해석 및 ORDER BY 오류
- SelectColumnResolveError
- ORDER BY의 AmbiguousReference
- ORDER BY의 TableNotSpecified
- ORDER BY의 ColumnNotExist

### SC-07 JOIN 정상/오류
- `select *` join 헤더(중복 컬럼명 alias prefix)
- JOIN 타입 불일치(Incomparable)
- JOIN 참조 컬럼 오류(ColumnNotExist)

### SC-08 LIMIT/OFFSET
- 정상 offset/limit 조합
- 음수 limit/offset 에러
- `offset ... limit ...` 구문 SyntaxError

### SC-09 GROUP BY 정상/오류
- 그룹별 MAX/MIN/SUM
- NULL-only 그룹 결과
- SelectColumnNotGrouped
- GROUP BY의 ambiguous/table-not-specified/column-not-exist

### SC-10 aggregate without GROUP BY
- 전체 집합 aggregate
- `sum(char)`는 0
- 날짜 MAX/MIN 비교

### SC-11 대소문자/alias/rename
- 식별자 대소문자 혼합 입력
- AS alias 사용
- rename/show tables 회귀 확인

### SC-12 문자열 내 세미콜론 + statement 분리
- `'abc;def'` 데이터 insert
- statement extractor 안정성 검증

### SC-13 NoSuchTable 메시지 3종
- INSERT/DELETE/SELECT 각각 no such table

### SC-14 TRUNCATE/DROP FK 보호
- 참조 중인 parent truncate/drop 실패
- child drop 후 parent drop 성공

### SC-15 UPDATE 비지원 동작 고정
- `update ...` 입력 시 Syntax error

---

## 자동 검증 명령

```bash
source Assignment1/.venv/bin/activate
python Assignment1/validate.py
bash Assignment1/run_scenarios.sh
bash Assignment1/verify_messages_definition.sh
```

PASS 기준:
- `validate.py`: grammar + runtime 확장 시나리오 모두 PASS
- `run_scenarios.sh`: 확장 edge scenario 전부 PASS
- `verify_messages_definition.sh`: 메시지 타입 검증 PASS
