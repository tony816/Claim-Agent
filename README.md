# Claim-Agent for Claude Code

화면과 실행기의 제품명은 `Claim-Agent`, 실행 명령은 `claim-agent`, Python 진입점은 `claim_agent`로 통일한다. 진행 중인 작업과 기존 명령의 호환성을 위해 이전 내부 구현 경로와 실행 별칭은 유지한다.

한국어 특허 청구항을 위한 프로젝트 전용 다중 에이전트 구성이다. `sources/`의 파일이 승인되었다는 사실만으로 현재 발명의 기술적 원자료 또는 검증된 긍정 예시가 되는 것은 아니며, 역할과 활성 시점은 `sources/README.md`에서 구분한다.

## 구성

- `CLAUDE.md`: 주 오케스트레이터의 우선순위, 활성 순서, 필수 검수 루프
- `claim-architect`: 주골격·근거·최소충분 한정 설계
- `dependent-claim-strategy-architect`: 독립항 LOCK 뒤 종속항 후보의 과제–특징–작동원리–효과를 검증하고 단순 도면 묘사를 배제
- `claim-drafter`: 잠긴 독립항 설계 또는 종속항 기술기여 계약을 보존해 `PRE_STYLE` 의미 초안 작성
- `claim-style-adjuster`: 의미 초안 뒤 `07_용어표현_출처게이트`와 `04_청구항_스타일가이드`를 적용해 범위 불변인 exact 문언과 `CLAIM_STYLE_GATE` 봉인
- `claim-success-reviewer`: style-adjuster가 봉인한 exact 독립항 또는 종속항 세트를 최신 성공조건·소스 카탈로그·원자료에 독립 대조해 success record 봉인
- `syntax-scope-reviewer`: 용어 출처·통사·명제 트리·부모항 체인·권리범위 감사
- `oa-strategy-reviewer`: 독립항 및 종속항 세트의 분리된 DRAFT·FINAL OA·회피설계 감사
- `blind-claim-reconstruction-reviewer`: 파일·검색·웹 도구 없이 독립항 또는 부모항 체인을 포함한 개별 종속항 문언만으로 비특허 기술 독자의 관계·형상 snapshot을 봉인하는 독립 감사
- `picture-claim-reconstruction-reviewer`: 봉인 snapshot을 DESIGN_GATE 또는 목표 `DC-NN`·DEPENDENT_DESIGN_GATE·도면과 비교하는 최종 관계·형상 감사

청구항 작성은 `AUTHORING_DRAFT`, 출원용 최종 검증은 `FINALIZATION`으로 분리한다. 독립항은 architect → drafter의 PRE_STYLE 의미 초안 → 별도 style adjuster의 용어·스타일 후처리 → 별도 success reviewer → syntax → OA → blind → 기준 비교 순서로 검수한다. 유효한 독립항 LOCK 뒤 종속항을 요청하면 별도 `dependent_set_id`에 대해 기술기여 설계 → PRE_STYLE 문언 작성 → 별도 dependent style 조정 → 별도 dependent success reviewer → syntax → OA → 종속항별 blind → 기준·도면 비교를 수행한다. `CLAIM_STYLE_GATE: PASS` 전에는 PRE_STYLE 초안을 exact revision으로 취급하지 않고, 유효한 success record 전에는 syntax로 진행하지 않으며, `DEPENDENT_DESIGN_GATE`가 잠기기 전에는 종속항을 작성하지 않고 `DEPENDENT_RECONSTRUCTION_GATE: PASS` 전에는 종속항 세트 LOCK을 만들지 않는다. PHYSICAL/HYBRID 문언은 일반 기계 개발자의 1회독 도식화와 실제 물체·면/관찰 단면·기준/단면 윤곽/형상 술어 주체의 분리까지 검사한다. 기술기여 후보가 없으면 형상 항으로 수를 채우지 않는다. 잠정·최종 종속항 세트는 각각 `DRAFT_DEPENDENT_SET_LOCK`과 `FINAL_DEPENDENT_SET_LOCK`으로 독립항 LOCK과 분리한다.

## Python + Gemini 런타임

**처음 쓰는 분은 [`사용법.md`](사용법.md)부터 보면 된다.** 설치·실행·결과 읽기·멈췄을 때 대처를 복사해 쓸 수 있는 명령으로 정리했다.

Claude Code 없이 같은 절차를 실행하는 독립 프로그램은 [`docs/python_runtime.md`](docs/python_runtime.md)를 따른다. `.claude/agents/*.md`를 그대로 system instruction으로 쓰고, 게이트 전제조건·record_id·revision 무효화·블라인드 격리·LOCK 조립을 Python이 결정론적으로 수행하며, Gemini API(기본 `gemini-3.8-flash`, `claim-agent.yaml`에서 변경)로 각 역할을 호출한다.

```bash
pip install -e . && claim-agent doctor --contracts
claim-agent run --request-yaml eval/cases/sample-clip-holder/request.yaml --replay eval/cases/sample-clip-holder/fixtures   # 오프라인 데모
claim-agent feedback · claim-agent runs rca <run_id> · claim-agent eval run --case … --shadow · claim-agent lessons propose --from-feedback   # 개선 루프
```

## 웹 단일 에이전트판

서브에이전트를 사용할 수 없는 웹 프로젝트에서는 [`web/PROJECT_INSTRUCTIONS.md`](web/PROJECT_INSTRUCTIONS.md)를 프로젝트 지침으로 사용한다. 이 어댑터는 기존 기술·통사·OA 기준을 유지하면서 역할을 한 에이전트가 순차 패스로 수행하게 한다.

- 한 대화 안에서 끝내는 `WEB_SINGLE_CHAT`은 `DRAFT-SELF` 독립항 LOCK 뒤 동일 문맥 순차 패스로 종속항 기술기여 게이트, 목표항별 self reconstruction·기준 비교 및 별도 DRAFT 종속항 세트 LOCK까지 만들 수 있다. 독립 검수로 표시하거나 FINAL LOCK으로 승격하지 않는다.
- 별도의 빈 대화에서 격리된 blind snapshot을 받는 `WEB_ISOLATED_CHATS`은 `DRAFT-ISOLATED` 잠금을 만들 수 있다.
- 서브에이전트 부재 자체는 `LOCK_MISSING_OR_STALE` 사유가 아니다. 실제 입력·문언·근거·게이트 결함만 중단 사유가 된다.
- 모든 기록은 `bundle_version`, `source_set_id`, `input_revision`, `design_revision`, `candidate_id`, `revision`으로 버전 고정된다.

웹 업로드용 묶음은 `scripts/build-web-bundle.ps1`로 생성한다. 결과물은 `dist/claim-agent-web-<버전>/`과 같은 이름의 ZIP 파일이다.
