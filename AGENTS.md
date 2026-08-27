# Claim Copa — Codex 오케스트레이션 규칙

## 기준 문서

- 이 프로젝트에서 작업하기 전에 루트의 `CLAUDE.md`를 끝까지 읽고, 그 문서의 소스 권한, 요청 유형, 게이트, revision, lock 및 답변 규칙을 프로젝트의 상세 작업 계약으로 적용한다.
- `.claude/agents/*.md`는 각 전문 역할의 실행 계약이다. 서브에이전트를 호출할 때는 대응하는 역할 파일을 끝까지 읽고 따르도록 명시한다.
- 이 파일은 기존 Claude 하네스를 변경하지 않고 Codex의 자동 위임 동작만 연결한다. 이 파일과 `CLAUDE.md`가 충돌하면 더 엄격한 검증·근거·잠금 규칙을 적용한다.

## 자동 서브에이전트 위임

- 사용자가 매번 `서브에이전트를 사용하라`고 말하지 않아도 된다.
- 요청을 `CLAUDE.md`의 `AUTHORING_DRAFT`, `FINALIZATION`, `REVIEW_ONLY`, `META` 중 하나로 먼저 분류한다.
- `AUTHORING_DRAFT` 또는 `FINALIZATION`이면, 서브에이전트 기능을 사용할 수 있는 한 아래 파이프라인을 주 에이전트가 질문이나 재승인 없이 자동으로 실행해야 한다.
- 주 에이전트가 전체 전문 검수를 혼자 수행한 뒤 다중 에이전트 실행으로 표현해서는 안 된다. 각 필수 단계는 실제 서브에이전트 호출과 반환 결과로 증명한다.
- 게이트 의존 단계는 아래 순서대로 실행한다. 서로 독립적인 자료 조사나 비교만 병렬화할 수 있다.
- 사용자가 실행 중지·취소 또는 특정 단계 생략을 명시하면 즉시 그 지시를 우선한다.

## 독립항 필수 역할과 순서

1. **설계:** `claim-architect` 역할의 서브에이전트를 호출하고 `.claude/agents/claim-architect.md`를 적용한다.
2. **작성:** 설계 결과가 `상태: PASS` 및 `DESIGN_GATE: LOCKED`일 때만 `claim-drafter` 역할의 서브에이전트를 호출하고 `.claude/agents/claim-drafter.md`를 적용한다.
3. **통사·범위 검수:** 성공조건 및 용어·표현 게이트를 통과한 동일 revision에 `syntax-scope-reviewer` 역할의 서브에이전트를 호출하고 `.claude/agents/syntax-scope-reviewer.md`를 적용한다.
4. **OA 검수:** syntax가 `PASS`인 변경 없는 동일 revision에 `oa-strategy-reviewer` 역할의 서브에이전트를 호출하고 `.claude/agents/oa-strategy-reviewer.md`를 적용한다.
5. **독립 블라인드 복원:** 해당 요청 모드의 OA 게이트가 역구성 진입 조건을 충족하면, 기존 설계 정답과 선행 토론을 전달하지 않은 새 컨텍스트의 `blind-claim-reconstruction-reviewer` 역할 서브에이전트를 호출하고 `.claude/agents/blind-claim-reconstruction-reviewer.md`를 적용한다. 가능한 경우 대화 이력을 상속하지 않는 fresh/non-fork 컨텍스트를 사용한다. PHYSICAL 또는 물리 관계를 포함한 HYBRID 청구항은 특허 문언 보정에 익숙한 실무자가 아니라 일반 기계 개발자가 청구항 평문을 한 번 읽고 도식화하는 독자 프로필을 적용한다.
6. **도면·기준 비교:** 봉인된 blind snapshot을 `picture-claim-reconstruction-reviewer` 역할의 별도 서브에이전트에 전달하고 `.claude/agents/picture-claim-reconstruction-reviewer.md`를 적용하여 DESIGN_GATE 및 허용된 원자료와 비교한다. 단면·축·가상선·영역이 등장하면 실제 물체·면, 관찰 단면·기준, 단면에 나타나는 윤곽 및 형상 술어의 귀속을 분리해 비교한다.
7. **종합:** 주 에이전트는 각 단계의 실제 결과, 식별자 및 PASS/REVIEW/BLOCK/UNVERIFIED 상태를 확인한 뒤에만 최종 결과를 종합한다.

문언 수정이 발생하면 `CLAUDE.md`가 정한 revision/design_revision 무효화 및 재실행 규칙에 따라 필요한 앞 단계로 돌아간다. 앞 단계의 PASS를 새 문언에 재사용하지 않는다.

## 종속항 요청 시 추가 필수 역할과 순서

AUTHORING_DRAFT에서는 유효한 `DRAFT_CLAIM_LOCK` 또는 `FINAL_CLAIM_LOCK`, FINALIZATION에서는 유효한 `FINAL_CLAIM_LOCK` 뒤 종속항 세트가 요청되면 아래 절차를 질문이나 재승인 없이 이어서 실행한다. 독립항 단계의 PASS는 루트 LOCK의 유효성만 증명하며 종속항 세트의 기술기여·통사·OA PASS를 대신하지 않는다.

1. **종속항 기술기여 설계:** 새 컨텍스트의 `dependent-claim-strategy-architect` 역할 서브에이전트를 호출하고 `.claude/agents/dependent-claim-strategy-architect.md`를 적용한다. 루트 LOCK, 현재 발명의 원자료, 선행기술 또는 `PRIOR_ART_SET: NONE`, `dependent_set_id` 및 `dependent_design_revision`을 전달한다.
2. **종속항 작성:** 설계 결과가 `상태: PASS` 및 `DEPENDENT_DESIGN_GATE: LOCKED`일 때만 `claim-drafter`를 `draft_scope: DEPENDENT_SET`으로 호출한다. drafter가 후보를 새로 고르거나 `DRAWING_ONLY`를 추가하도록 맡기지 않는다.
3. **종속항 통사·범위 검수:** 동일 `dependent_revision`의 종속항 세트에 `syntax-scope-reviewer`를 `review_scope: DEPENDENT_SET`으로 호출한다. 부모항을 합친 전체 발명, 인용관계, 선행기재, 기술기여 계약 보존 및 효과 문구의 수단 대체 여부를 검수한다.
4. **종속항 OA 검수:** syntax가 `PASS`인 변경 없는 동일 `dependent_revision`에 `oa-strategy-reviewer`를 `review_scope: DEPENDENT_SET`으로 호출한다. `DEPENDENT_OA_DRAFT_GATE`와 `DEPENDENT_OA_FINAL_GATE`를 분리하여 받는다.
5. **종속항별 블라인드 복원:** 해당 요청 모드의 종속항 OA 게이트가 역구성 진입 조건을 충족하면, 각 종속항을 서로 독립된 fresh/non-fork 컨텍스트의 `blind-claim-reconstruction-reviewer`에 `claim_scope: DEPENDENT_SINGLE`로 호출한다. 입력에는 해당 목표 종속항의 정확한 부모항 체인과 목표항 문언만 전달하고, 설계 정답·도면·기술기여 설명·선행 토론은 전달하지 않는다. 각 에이전트는 비특허 기술 독자가 문언만으로 추가 형상·관계를 한 번에 도식화할 수 있는지 봉인한다. 형제 종속항은 같은 blind 컨텍스트에 묶지 않는다.
6. **종속항별 도면·기준 비교:** 각 봉인 snapshot을 별도의 `picture-claim-reconstruction-reviewer`에 `claim_scope: DEPENDENT_SINGLE`로 전달한다. 부모항 전체 조합, 해당 `DC-NN` 의미 한정 패키지, `DEPENDENT_DESIGN_GATE` 및 허용된 원자료와 비교하고, 모든 목표항이 `PASS` 또는 `PASS-RANGE`이며 `NON_PATENT_TECHNICAL_READER_GATE`와 해당하는 `GEOMETRIC_OBJECT_GATE`를 통과한 경우에만 `DEPENDENT_RECONSTRUCTION_GATE: PASS`로 종합한다. 형상·공간 표현이 없는 목표항의 GEOMETRIC_OBJECT_GATE는 `NOT_APPLICABLE`로 기록한다.
7. **종속항 세트 종합·LOCK:** 동일 문언의 기술기여·TERM_EXPRESSION_GATE·success·syntax·해당 OA 게이트 및 `DEPENDENT_RECONSTRUCTION_GATE`가 모두 PASS인 경우에만 `DRAFT_DEPENDENT_SET_LOCK` 또는 `FINAL_DEPENDENT_SET_LOCK`을 기록한다. 선행기술이 없으면 진보성은 `UNVERIFIED`로 봉인하며 PASS로 바꾸지 않는다.

종속항 문언이 바뀌면 새 `dependent_revision`으로 종속항 success·syntax·OA·종속항별 blind snapshot·picture 비교를 모두 다시 실행한다. 후보 집합, 과제–특징–원리–효과, 부모항 전략, 형상·공간 객체 계약, 근거 원자료 또는 선행기술 집합이 바뀌면 새 `dependent_design_revision`으로 `dependent-claim-strategy-architect`부터 다시 실행한다. 루트 독립항이나 그 LOCK이 바뀌면 종속항 산출물과 LOCK 전체를 무효화하고 독립항의 필요한 앞 단계로 돌아간다.

## 제한 검토와 설정 작업

- `REVIEW_ONLY`에서는 요청된 검수 역할만 실제 서브에이전트에 위임한다. 단순 비교나 특정 OA 항목 검토를 전체 작성 파이프라인으로 확대하지 않는다.
- `META`에서는 실제 청구항 후보 검수가 없는 한 청구항 작성·검수 서브에이전트를 호출하지 않는다.
- 설정 또는 에이전트 역할 자체를 수정하라는 요청에서는 변경 대상과 무관한 청구항 파이프라인을 실행하지 않는다.

## 호출 계약과 실패 처리

- 각 호출에는 최소한 요청 모드, 역할 파일 경로, 작업 범위, 사용 가능한 원자료의 정확한 경로, USER_LOCK, candidate_id, revision, design_revision 및 직전 게이트 결과를 필요한 범위에서 전달한다. 종속항 단계에는 `dependent_set_id`, `dependent_design_revision`, 현재 또는 목표 `dependent_revision`, 루트 LOCK 전문, 선행기술 경로 또는 `PRIOR_ART_SET: NONE` 및 직전 종속항 게이트 결과도 전달한다. 종속항 blind 호출에는 예외적으로 원자료·설계·게이트 결과를 전달하지 않고 목표항 식별자, 정확한 부모항 체인 및 목표항 문언만 전달한다.
- 서브에이전트에게 다른 에이전트도 같은 작업공간을 사용할 수 있음을 알리고, 다른 작업자의 변경을 되돌리지 않도록 한다.
- 필수 입력이 없으면 해당 역할 파일과 `CLAUDE.md`의 규칙대로 `UNVERIFIED`, `REVIEW` 또는 `BLOCK`으로 남긴다. 추측으로 PASS를 만들지 않는다.
- 서브에이전트 기능을 사용할 수 없거나 호출이 실패하면 실행한 것처럼 보고하지 않는다. 누락된 단계, 실패 원인 및 남은 검증을 최종 답변에 명시한다.
- 사용자가 별도로 요구하지 않는 한 외부 송부, 이메일 전송, 배포 또는 원자료 삭제를 자동 위임 범위에 포함하지 않는다.
