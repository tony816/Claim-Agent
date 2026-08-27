<!-- claim-copa-bundle: 2026.08.27.3 -->

# Claim Copa Web 설치·사용법

이 묶음은 서브에이전트 기능이 없는 웹 프로젝트에서 Claim Copa를 실행하기 위한 단일 에이전트 어댑터다. 기존 다중 에이전트판의 기술적 근거 기준과 청구항 게이트를 낮추지 않고, 호출 단계를 한 에이전트의 명시적 순차 패스로 바꾼다.

## 설치

1. `dist/claim-copa-web-2026.08.27.3.zip`의 압축을 푼다.
2. 웹 프로젝트의 프로젝트 지침에 `PROJECT_INSTRUCTIONS.md` 전문을 넣는다.
3. 나머지 `VERSION`, `core/`, `roles/`, `sources/`, `HANDOFF_TEMPLATES.md`를 프로젝트 소스로 올린다.
4. `MANIFEST.sha256`도 함께 올려 현재 묶음의 파일 집합을 식별한다.
5. 구버전 파일과 신버전 파일을 한 프로젝트에 섞지 않는다.

프로젝트가 파일별 해시를 직접 계산하지 못해도 각 Markdown 파일 첫 줄의 `claim-copa-bundle` 표지와 `VERSION`의 `source_set_id`가 모두 일치하는지 확인할 수 있다. 빌드가 실제 포함 파일에서 계산한 `BUNDLE_MANIFEST.md`의 `source_manifest_digest`는 모든 RUN_HEADER와 LOCK에 함께 기록한다. 버전이 섞였으면 청구항 작업을 시작하지 말고 한 버전의 파일로 다시 올린다.

## 실행 프로필

### WEB_SINGLE_CHAT

한 대화에서 독립항 architect → drafter → success → syntax → OA → self reconstruction → reference compare를 순차 수행한다. 모든 내용 게이트가 통과하면 `DRAFT_CLAIM_LOCK`의 `lock_class`를 `DRAFT-SELF`로 기록한다. 종속항이 요청되면 이어서 dependent strategy → drafter → dependent success → syntax → OA → 목표 종속항별 self reconstruction → reference compare를 별도 식별자로 수행한다. `DEPENDENT_DESIGN_GATE: LOCKED` 전에는 문언을 작성하지 않고 `DEPENDENT_RECONSTRUCTION_GATE: PASS` 전에는 종속항 세트 LOCK을 만들지 않는다.

이 프로필의 역구성과 종속항 검수는 독립 검수가 아니다. 결과에는 항상 `동일 문맥 자체검수`라고 표시하고, `FINAL_CLAIM_LOCK` 또는 `FINAL_DEPENDENT_SET_LOCK`으로 승격하지 않는다.

### WEB_ISOLATED_CHATS

작성 대화와 별도의 빈 대화를 사용한다. blind 단계는 프로젝트 소스가 연결되지 않은 새 대화에서 `BLIND_CHAT_PROMPT.md`와 허용 입력만 제공해 수행한다. 종속항은 목표항마다 새 빈 대화를 사용하고 정확한 부모항 체인과 목표항만 전달한다. 봉인 결과를 원래 대화에 가져와 reference compare를 수행한다. 이 경우 `lock_class: DRAFT-ISOLATED`로 기록할 수 있다.

syntax와 OA까지 별도 빈 대화에서 versioned handoff로 검수하고 정식 명세서 기준 `OA_FINAL_GATE: PASS`를 얻은 경우에만 웹 경로에서 `FINAL_CLAIM_LOCK`을 검토할 수 있다. 최종 종속항 세트는 유효한 FINAL_CLAIM_LOCK, 별도 dependent syntax·`DEPENDENT_OA_FINAL_GATE: PASS`, 목표항별 격리 blind·기준 비교 및 `DEPENDENT_RECONSTRUCTION_GATE: PASS`가 있어야 한다.

## 가장 짧은 사용 예

프로젝트 대화에 다음처럼 요청한다.

```text
request_mode: AUTHORING_DRAFT
execution_profile: WEB_SINGLE_CHAT
USER_LOCK: 청구항 1 전문
목표: 청구항 1을 변경하지 않고 잠정 종속항 2~10 작성
현재 발명 원자료: 첨부 파일 A, 도면 B, 관계표 C
PRIOR_ART_SET: NONE
```

서브에이전트 호출이 없다는 이유만으로 중지하면 설치가 잘못된 것이다. 다만 USER_LOCK 자체의 복수 대응, 선행기재, 기술관계 또는 원자료 근거가 실제로 닫히지 않으면 그 내용 결함은 계속 `REVIEW` 또는 `BLOCK`이다.

## 버전 변경

`web/VERSION`의 값을 올린 뒤 `scripts/build-web-bundle.ps1`을 실행한다. 기존 게이트 기록이나 LOCK은 새 `bundle_version` 또는 `source_set_id`와 일치하지 않으면 재사용하지 않는다.
