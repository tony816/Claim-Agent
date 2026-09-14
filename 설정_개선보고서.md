# Claim-Agent 설정 개선보고서

작성일: 2026-08-26

> **이력 문서:** 이 보고서는 DRAFT/FINAL 독립항 LOCK 분리를 도입한 당시의 기록이다. 2026-08-27 이후 종속항 단계에는 `sources/08_종속항_기술기여_게이트.md`와 `dependent-claim-strategy-architect`가 추가되었으므로, 아래의 `DRAFT_CLAIM_LOCK → 종속항 작성` 표현은 현재 실행 계약이 아니다. 현재 기준은 `CLAUDE.md`, `AGENTS.md` 및 `sources/README.md`를 따른다.

## 1. 결론

설정을 수정했다.

이제 **정식 명세서가 아직 없다는 이유만으로 종속항 작성 전체가 멈추지 않는다.** 현재 발명의 기술 설명, 도면 또는 관계표에 청구항의 근거가 충분하면 잠정 독립항을 잠그고 잠정 종속항까지 계속 작성한다.

다만 정식 명세서에서 뒷받침과 실시가능성을 확인하기 전에는 그 결과를 출원용 최종안이라고 부르지 않는다.

## 2. 무엇이 잘못되어 있었나

기존 흐름은 다음 두 판단을 사실상 하나로 묶었다.

1. 지금 작성한 청구항 문장이 기술 설명과 맞는가?
2. 완성된 정식 명세서가 그 청구항을 법적으로 충분히 뒷받침하는가?

두 번째 판단은 정식 명세서가 없으면 확인할 수 없다. 그런데 이 미확인 상태가 OA 전체 `REVIEW`로 이어졌고, `FINAL_CLAIM_LOCK`이 만들어지지 않아 종속항 가이드도 실행되지 않았다.

그래서 문언 결함이 0건이어도 다음과 같이 멈췄다.

```text
정식 명세서 없음
→ OA 종합 REVIEW
→ FINAL_CLAIM_LOCK 없음
→ 종속항 전개 금지
→ 사용자에게 예외 선택 질문
```

이것은 “잠정 작성”과 “출원용 최종 검증”을 구분하지 않은 것이 원인이었다.

## 3. 어떻게 바꾸었나

### 3.1 작성 목적을 둘로 나눔

- `AUTHORING_DRAFT`: 정식 명세서가 완성되기 전의 청구항 작성과 잠정 종속항 전개
- `FINALIZATION`: 정식 명세서·도면·최초 출원자료까지 대조하는 출원용 최종 검증

사용자가 출원용 최종안을 명시하지 않으면 기본적으로 `AUTHORING_DRAFT`로 진행한다.

### 3.2 OA 판단을 둘로 나눔

- `OA_DRAFT_GATE`: 청구항 문언, 구조, 선행기재와 현재 제공된 기술 원자료가 서로 맞는지 확인
- `OA_FINAL_GATE`: 정식 명세서의 뒷받침과 실시가능성까지 확인

정식 명세서가 없을 때 결과는 다음처럼 분리된다.

```text
OA_DRAFT_GATE: PASS
OA_FINAL_GATE: UNVERIFIED — SPEC_NOT_PROVIDED
```

따라서 최종 검증은 아직 끝나지 않았지만 잠정 작성은 계속할 수 있다.

### 3.3 잠금을 둘로 나눔

- `DRAFT_CLAIM_LOCK`: 현재 기술 원자료와 문언 검수를 통과한 잠정 독립항 잠금
- `FINAL_CLAIM_LOCK`: 정식 명세서의 뒷받침·실시가능성까지 확인한 최종 잠금

`DRAFT_CLAIM_LOCK`이 생기면 종속항 작성기를 `draft_scope: DEPENDENT_SET`으로 다시 실행한다. 이때 결과에는 반드시 다음 표시를 붙인다.

```text
명세서 뒷받침·실시가능성 미검증 잠정안
```

### 3.4 역구성 검수를 그대로 유지함

잠정안이라고 해서 역구성 검수를 생략하지 않는다.

1. 별도의 blind 에이전트가 청구항 문언만 보고 관계를 복원한다.
2. `picture-claim-reconstruction-reviewer`가 그 복원 결과를 설계 기준과 비교한다.
3. `PASS` 또는 `PASS-RANGE`여야 `DRAFT_CLAIM_LOCK`을 만들 수 있다.

또한 picture reviewer가 실행될 때 요청 유형에 맞는 OA 보고서 전문을 필수 입력으로 받도록 바꾸었다. 따라서 OA 단계를 건너뛴 잘못된 호출은 `UNVERIFIED — UPSTREAM_OA_GATE_NOT_PASS`로 중지된다.

## 4. 현재 작업 흐름

```text
발명 기술 설명·도면·관계표
        ↓
설계자: 주골격과 기술관계 확정
        ↓
독립항 작성
        ↓
성공조건 검사 → 통사 검사
        ↓
OA_DRAFT_GATE
   ├─ 실패 → 새 revision으로 수정 후 재검사
   └─ PASS
        ↓
blind 관계 복원
        ↓
picture reviewer가 설계 기준과 비교
        ↓
DRAFT_CLAIM_LOCK
        ↓
잠정 종속항 작성
```

정식 명세서가 제공된 최종 검증은 아래 단계가 더 붙는다.

```text
OA_FINAL_GATE: PASS
        ↓
FINAL_CLAIM_LOCK
        ↓
출원용 최종 종속항 작성·검수
```

## 5. 실제 적용되는 핵심 설정 문구

프로젝트 지휘 규칙에는 다음 취지의 문구를 넣었다.

> 정식 명세서 부재로 실시가능성 또는 뒷받침을 확인할 수 없으면 `OA_FINAL_GATE: UNVERIFIED — SPEC_NOT_PROVIDED`로 두되 DRAFT 게이트를 REVIEW로 낮추지 않는다.

OA 검수 에이전트에는 다음 문구를 넣었다.

> 정식 명세서가 제공되지 않았다는 사실만으로 `OA_DRAFT_GATE`를 `REVIEW` 또는 `BLOCK`으로 낮추지 않고, 사용자에게 예외 선택을 요구하지 않는다.

종속항 작성기에는 다음 실행 조건을 넣었다.

> `DRAFT_CLAIM_LOCK` 기반 결과에는 `명세서 뒷받침·실시가능성 미검증 잠정안`을 표시하고, `FINAL_CLAIM_LOCK` 없이는 출원용 최종안이라고 표시하지 않는다.

## 6. 질문에 나온 상황은 이제 어떻게 처리되나

질문의 상황은 다음과 같았다.

- 문언 기인 결함: 0건
- 정식 명세서: 없음
- 현재 발명의 기술 설명 또는 도면: 있음

이제 설정상 결과는 다음과 같다.

```text
OA_DRAFT_GATE: PASS
OA_FINAL_GATE: UNVERIFIED — SPEC_NOT_PROVIDED
DRAFT 역구성 진행 가능: YES
FINAL_CLAIM_LOCK 가능: NO
```

그 뒤 blind 검수와 picture 비교가 통과하면 `DRAFT_CLAIM_LOCK`을 만들고, 사용자에게 “예외적으로 진행할까요?”라고 묻지 않은 채 잠정 종속항을 작성한다.

단, 현재 발명의 기술 설명·도면·관계표에도 어떤 한정의 근거가 없다면 그 부분은 여전히 `REVIEW` 또는 `BLOCK`이다. 명세서가 없다는 이유만 없애 준 것이지, 기술 근거가 없는 내용을 만들어도 된다는 뜻은 아니다.

## 7. 수정한 설정 범위

- 프로젝트 전체 흐름과 요청 유형
- 독립항 설계자와 작성기 입력 계약
- 통사 검수와 OA 검수 계약
- blind 이후 picture 비교 실행 조건
- 잠정·최종 잠금 규칙
- 종속항 가이드 활성 시점과 잠정안 표시
- 독립항 성공조건과 OA 체크리스트
- 루트 안내문과 소스 안내문

## 8. 확인 결과

- 에이전트 설정 파일 6개의 머리말 형식 확인: 통과
- Markdown 파일 UTF-8 읽기 확인: 통과
- 폐기된 이전 요청 유형 문구 잔존 여부: 없음
- `DRAFT_CLAIM_LOCK`, `OA_DRAFT_GATE`, `OA_FINAL_GATE` 필수 계약 존재 여부: 통과
- picture reviewer의 OA 선행 게이트 강제 여부: 통과
- 종속항 작성기의 `DEPENDENT_SET` 실행 계약 존재 여부: 통과

## 9. 추가 대화에 대한 재발 방지

추가 대화에서는 다음과 같이 잘못 설명했다.

```text
OA가 REVIEW이므로 blind와 picture를 실행하지 않았다.
원하면 REVIEW_ONLY 진단으로 돌릴 수 있다.
```

정식 명세서 부재만이 원인이었다면 이 설명은 새 설정에서 허용되지 않는다. 올바른 처리는 다음과 같다.

```text
OA_DRAFT_GATE: PASS
OA_FINAL_GATE: UNVERIFIED — SPEC_NOT_PROVIDED
→ blind 자동 실행
→ picture 자동 실행
→ 통과하면 DRAFT_CLAIM_LOCK
→ 잠정 종속항 전개
```

이를 위해 프로젝트 지휘 규칙에 `역피처 자동 실행 하드 규칙`을 추가했다. 자동 실행 조건이 충족되면 `돌려볼까요?`라고 묻거나 `REVIEW_ONLY`로 낮출 수 없다. OA 에이전트도 두 게이트를 하나의 종합 REVIEW로 합칠 수 없도록 금지 문구를 추가했다.

## 10. 2026-08-27 용어·표현 출처 게이트 추가

이미지처럼 구성 명칭이 없는 원자료에서는 설계자가 붙인 임시 이름이 그대로 최종 청구항 용어로 잠기는 문제가 있었다. 그 결과 기술관계 검수는 통과해도 `팔부`, `평판형`, `길이방향`, `허브부를 매개로`처럼 프로젝트 소스 원문의 용어·표현과 거리가 있는 문언이 남을 수 있었다.

이를 다음과 같이 수정했다.

1. DESIGN_GATE의 `기술 개념`과 최종 청구항의 `표면 용어`를 분리했다.
2. 현재 발명 원자료나 USER_LOCK에 실제 명칭이 있으면 `SOURCE_EXACT_TERM`, 이미지에 임시로 붙인 이름이면 `CONCEPT_LABEL_ONLY`로 표시한다.
3. 의미 초안 뒤에 `TERM_EXPRESSION_GATE`를 추가했다.
4. 모든 주요 구성명·관계 술어·형상 표현·띄어쓰기에 출처 라벨과 정확한 소스 위치를 요구한다.
5. `길이방향 → 길이 방향`, 무근거 `○○형 → ○○ 형상` 등 프로젝트 기본 정규화를 추가했다.
6. `팔부 → 복수의 날개부`처럼 용어 수정이 구성 계층이나 권리범위를 바꾸면 문체 교정으로 처리하지 않고 새 design revision의 architect 단계로 되돌린다.
7. syntax, OA, picture 비교 및 DRAFT·FINAL lock이 동일 revision의 `TERM_EXPRESSION_GATE: PASS`를 요구하도록 입력 계약을 연결했다.

새 활성 순서는 다음과 같다.

```text
기술 개념·구성 계층 DESIGN_GATE
        ↓
보조 예시 없는 의미 초안
        ↓
용어·표현 출처표 + TERM_EXPRESSION_GATE
        ↓
success → syntax → OA → blind → reference compare
```

따라서 소스 원문과 다른 표현이 필요하면 이를 숨기지 않고 `NEW_NEUTRAL_TERM`과 선택 이유를 남긴다. 용어·표현 출처표가 없거나 즉석 조어가 무표시로 남아 있으면 syntax 단계로 진행할 수 없다.

## 11. 2026-08-27 종속항 역피처·비특허 기술 독자 게이트 추가

수동 검토에서 종속항의 기술기여와 통사 자체는 설명할 수 있어도, 특허 문언에 익숙하지 않은 일반 기계 개발자가 청구항을 읽고 형상을 그리기 어려운 문제가 확인되었다. 특히 `단면에서 오목면을 포함하는`과 같은 표현은 가상의 관찰 단면과 실제 면의 존재 위치를 혼동시킬 수 있었고, 양단부·가상선·중앙부를 누적 정의한 문언은 기본 형상을 지나치게 우회해 설명했다.

직접 원인은 독립항에만 `blind → picture` 역피처가 있었고 종속항은 `syntax → OA → LOCK`에서 끝났다는 점이다. 이를 다음과 같이 수정했다.

1. 각 종속항을 정확한 부모항 체인과 합쳐 별도의 fresh/non-fork blind context에서 복원한다.
2. 형제 종속항, 설계 정답, 도면, `DC-NN` 설명 및 선행 피드백을 blind 입력에서 제외한다.
3. PHYSICAL/HYBRID의 독자 프로필을 특허 실무자가 아닌 일반 기계 개발자로 고정하고 `1회독 도식화 가능성`을 봉인한다.
4. `실제 물체·면 / 관찰 단면·기준 / 단면에 나타나는 외곽선·윤곽 / 형상 술어의 귀속 주체 / 방향·개방 대상`을 형상·공간 객체 계약과 검수표로 분리한다.
5. picture reviewer가 각 종속항 snapshot을 해당 `DC-NN`, DEPENDENT_DESIGN_GATE 및 허용 원자료와 비교한다.
6. 모든 목표항의 picture 판정이 PASS 또는 PASS-RANGE이고 `NON_PATENT_TECHNICAL_READER_GATE`와 `GEOMETRIC_OBJECT_GATE`가 PASS일 때만 `DEPENDENT_RECONSTRUCTION_GATE: PASS`로 종합한다.
7. `DRAFT_DEPENDENT_SET_LOCK`과 `FINAL_DEPENDENT_SET_LOCK`은 위 게이트와 종속항별 snapshot·비교 기록을 필수로 봉인한다.

새 종속항 활성 순서는 다음과 같다.

```text
DEPENDENT_DESIGN_GATE
        ↓
dependent drafter + TERM_EXPRESSION_GATE
        ↓
dependent success → syntax → OA
        ↓
각 종속항별 blind(부모항 체인 + 목표항만)
        ↓
각 종속항별 picture/DC·도면 비교
        ↓
DEPENDENT_RECONSTRUCTION_GATE
        ↓
DRAFT 또는 FINAL DEPENDENT_SET_LOCK
```

이 게이트는 문장을 무조건 짧게 만드는 규칙이 아니다. 필요한 권리 경계는 유지하되, 관찰 기준과 실제 형상 객체를 문법적으로 분리하고 비특허 기술자가 문언만으로 같은 구조를 복원할 수 있게 하는 규칙이다.

## 12. 2026-08-27 후반 스타일 조정 에이전트 분리

`04_청구항_스타일가이드.md`는 후처리 자료였지만 실제 실행 계약에서는 `claim-drafter`가 의미 초안 작성과 스타일 적용을 한 컨텍스트에서 모두 수행했다. 따라서 후반 문체 조정이 독립 단계로 증명되지 않았고, 의미 작성자가 자신의 스타일 적용을 스스로 PASS로 만들 수 있었다.

이를 다음과 같이 분리했다.

1. `claim-drafter`는 `07_용어표현_출처게이트.md`와 `04_청구항_스타일가이드.md`를 읽지 않고 `PRE_STYLE — NOT_GATE_ELIGIBLE` 의미 초안만 만든다.
2. 새 `claim-style-adjuster`가 별도 컨텍스트에서 두 후처리 문서를 적용한다.
3. style adjuster는 적용 전후 문언, 변경 규칙, 개념·계층·명제 트리·권리범위 영향 및 출처를 변경 대조표로 남긴다.
4. `style_record_id`, `CLAIM_STYLE_GATE: PASS`, `TERM_EXPRESSION_GATE: PASS` 및 최종 독자·기하 게이트가 봉인되어야 target revision이 success 단계에 진입한다.
5. 조사·띄어쓰기·문장부호·범위 불변 표면 용어만 바꾸면 `STYLE_ONLY_REVISION`을 사용할 수 있지만, 절 결속이나 기술관계 문언은 drafter로, 설계 계약 변경은 architect로 되돌린다.
6. 독립항과 종속항 모두 style → success → syntax → OA → blind → picture 순서를 강제하고, LOCK에 style record와 게이트를 봉인한다.

새 기본 흐름은 다음과 같다.

```text
DESIGN_GATE
        ↓
claim-drafter: PRE_STYLE 의미 초안
        ↓
claim-style-adjuster: 07 + 04 + 범위 불변
        ↓
CLAIM_STYLE_GATE + TERM_EXPRESSION_GATE
        ↓
success → syntax → OA → blind → reference compare → LOCK
```

웹 단일 에이전트판도 같은 역할 파일을 별도 순차 패스로 적용하도록 프로토콜을 `1.3.0`, 묶음 버전을 `2026.08.27.4`로 올렸다. 웹에서는 같은 에이전트가 수행했다는 사실을 숨기지 않지만, 의미 작성 패스와 스타일 패스의 기록·게이트는 분리한다.

## 13. 2026-08-27 소스 소비 공백과 성공조건 독립 검수 보완

후반 스타일 조정 역할을 분리한 뒤 소스별 실제 소비자를 역추적한 결과, 최종 exact 문언에 `success_record_id` 또는 `dependent_success_record_id`를 발급하는 전담 역할이 없고 일부 역할의 필수 로딩 목록이 실제 사용 소스를 빠뜨린 문제가 확인되었다. 주 오케스트레이터의 자체 종합은 독립 검수의 실행 증거가 될 수 없으므로 다음과 같이 보완했다.

1. 새 `claim-success-reviewer`를 추가해 style adjuster가 봉인한 exact 문언만 읽고 성공조건을 독립 재검사하게 했다. 이 역할만 독립항 `success_record_id`와 종속항 `dependent_success_record_id`를 발급한다.
2. reviewer가 `sources/README.md`, 최신 `독립항_작성_성공조건.md`, 07, 04와 실제 원자료를 필수로 읽고, 종속항이면 08과 05까지 직접 읽도록 했다.
3. `claim-style-adjuster`가 조건부 예시 검색을 활성화할 때 읽어야 할 정확한 경로를 `sources/청구항_예시검색_라우팅인덱스.md`와 `sources/청구항_문체학습용_분야별검색최적화본.md`로 고정했다. 검색이 필요하지 않으면 `조건부 보조 소스: NOT_ACTIVATED`를 기록한다.
4. dependent syntax reviewer가 `05_종속항_전개패턴_가이드.md`를 직접 읽고 부모항·인용관계·선행기재를 독립 검증하도록 했다.
5. architect, dependent architect, style adjuster 및 success reviewer가 공식 소스 카탈로그인 `sources/README.md`를 읽고 활성 순서와 기술내용 근거 금지를 확인하도록 했다.
6. AGENTS·CLAUDE·역할 파일·소스 문서·웹 어댑터·인계 템플릿·번들 검증 스크립트를 같은 계약으로 동기화했다.

새 기본 흐름은 다음과 같다.

```text
DESIGN_GATE
        ↓
claim-drafter: PRE_STYLE 의미 초안
        ↓
claim-style-adjuster: source catalog + 07 + 04
        ↓
CLAIM_STYLE_GATE + TERM_EXPRESSION_GATE
        ↓
claim-success-reviewer: 최신 성공조건 + 실제 원자료 독립 재검사
        ↓
success record → syntax → OA → reconstruction → reference compare → LOCK
```

종속항은 dependent style 뒤 같은 전담 reviewer가 08·05와 exact 세트를 대조해 dependent success record를 만들고, 그 뒤 syntax reviewer가 05를 다시 읽는다. 웹 단일 에이전트판은 독립 에이전트를 만들 수 없으므로 같은 역할 파일을 별도 순차 패스로 적용하고 `review_context: SAME_AGENT_SEQUENTIAL`을 숨기지 않는다. 이 변경으로 웹 프로토콜은 `1.4.0`, 묶음 버전은 `2026.08.27.5`가 되었다.
