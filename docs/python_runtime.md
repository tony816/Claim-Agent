# Claim Copa Python + Gemini 런타임

`claim_copa/`는 `CLAUDE.md`의 19단계 다중 에이전트 절차를 Claude Code 없이 실행하는 독립 프로그램이다. 역할 프롬프트는 계속 `.claude/agents/*.md`가 단일 출처이며(frontmatter만 제거해 system instruction으로 사용), 소스 권한은 `sources/README.md`를 그대로 따른다. 실행 프로필은 `PY_GEMINI_MULTI_CALL`이고 LOCK의 `lock_class`는 `DRAFT-ISOLATED-PY`다.

핵심 차이는 **부기(簿記)를 LLM이 아니라 Python이 한다**는 점이다.

| 항목 | Claude Code 판 | Python 런타임 |
|---|---|---|
| 게이트 전제조건·단계 순서 | 주 오케스트레이터 LLM이 판단 | `models/contracts.py`의 PASS 술어 + `pipeline/transitions.py`가 결정론적으로 판단 |
| record_id 발급 | 각 역할이 작성 | Python이 예정 ID를 지정, 역할이 PASS일 때만 발급으로 기록 |
| revision 무효화 | 규칙 문서 | exact 문언 sha256 스탬프; 1자라도 다르면 새 revision 강제 |
| 블라인드 격리 | fresh 서브에이전트 | 소스·설계·캐시 없는 별도 호출, `BlindPacket`이 허용 필드 외 직렬화 불가 |
| 종속항별 역구성 | 순차 서브에이전트 | 목표항별 blind→picture를 스레드 팬아웃(`pipeline.max_concurrency`) |
| 소스 파일 | Read/Glob | 역할×scope 화이트리스트로 사전 로딩 + Gemini context cache(TTL) |
| 역할 출력 | 마크다운 | JSON 봉투(게이트·상태·문언) + `report_markdown`(원 출력 형식 전문) — 둘을 상호 대조 |

## 설치

```bash
pip install -e .            # google-genai, pydantic, pyyaml
export GEMINI_API_KEY=...   # 없으면 --replay만 가능
claim-copa doctor --contracts      # 키·소스·역할·스키마 점검 (+ --live로 모델·JSON·캐시 프로브)
claim-copa models                  # 실제 사용 가능한 모델 ID 확인 → claim-copa.yaml의 model.default 수정
```

기본 모델 ID `gemini-3.8-flash`는 설정값일 뿐 확인된 값이 아니다. `claim-copa models`로 목록을 보고 `claim-copa.yaml`을 고친다. `thinking_level`을 지원하지 않는 모델이면 provider가 자동으로 `thinking_budget`으로 폴백하고, 캐시 최소 토큰 미달이면 인라인 전송으로 폴백한다.

## 실행

```bash
# 잠정 독립항 + 잠정 종속항 세트 (AUTHORING_DRAFT)
claim-copa run --mode AUTHORING_DRAFT --request-file req.md \
  --source 발명설명.md --drawing 도1.png --prior-art NONE --dependent --dependent-target "2~8"

# request.yaml 하나로 실행 (eval/cases/*/request.yaml 형식)
claim-copa run --request-yaml eval/cases/sample-clip-holder/request.yaml

# 오프라인 리플레이 (API 키 없이 파이프라인 동작 확인)
claim-copa run --request-yaml eval/cases/sample-clip-holder/request.yaml \
  --replay eval/cases/sample-clip-holder/fixtures

# 청구항만 출력
claim-copa runs report <run_id> --claims-only
```

결과는 `runs/<run_id>/`에 남는다: `state.json`, `calls/NNN-<role>-<scope>.{json,md}`(패킷·응답·봉투·보고서 전문), `records/<record_id>.{json,md}`(LOCK 포함), `telemetry.jsonl`, `report.md`.

종료 코드: 0 LOCK 도달 / 2 REVIEW 중지 / 3 BLOCK·UNVERIFIED 중지 / 4 루프 한도 / 5 봉투 무효 / 1 오류.

### 중지와 재개

역할이 라우팅 불가한 REVIEW/BLOCK/UNVERIFIED를 반환하면 즉시 중지하고 `report.md`의 `남은 REVIEW/BLOCK/UNVERIFIED`에 쟁점과 재개 명령을 적는다. 사용자가 결정의 성격을 분류한다.

```bash
claim-copa resume <run_id> --decide "오목면의 주체는 기둥 둘레면"          # 같은 단계 재실행(결정 문구를 패킷에 첨부)
claim-copa resume <run_id> --decide "길이 방향으로 띄어쓰기" --apply-style-fix   # STYLE_ONLY_REVISION, r+1
claim-copa resume <run_id> --decide "각각의 분배 원천 명시" --apply-meaning-fix # DRAFTER_REVISION, r+1
claim-copa resume <run_id> --decide "…" --redesign                            # design_revision d+1, architect부터
claim-copa resume <run_id> --add-source 추가도면.png                           # input_revision 변경 → d+1
claim-copa resume <run_id> --accept-unverified                               # 비게이팅 UNVERIFIED만 허용
```

`RETURN_TO_*`는 자동으로 처리된다(STYLE_ONLY 2회, DRAFTER 2회, ARCHITECT 1회, DEPENDENT_ARCHITECT 1회 기본). 한도를 넘으면 `LOOP_LIMIT`로 중지한다. `OA_FINAL_GATE: UNVERIFIED — SPEC_NOT_PROVIDED`는 AUTHORING_DRAFT의 역구성을 막지 않는다(하드 규칙이 코드에 있음). 소스·역할 파일·승인 교훈이 바뀌면 `source_set_id`가 바뀌고 이전 run은 STALE로 표시되어 `--restart-from ARCHITECT` 없이는 재개할 수 없다.

### REVIEW_ONLY

```bash
claim-copa review --reviewers syntax-scope-reviewer,oa-strategy-reviewer --claim-file claims.md --scope INDEPENDENT
```
`r1`, `design_revision: N/A`로 요청된 검수자만 호출하며 LOCK을 만들지 않는다.

## 설정 (`claim-copa.yaml`)

역할별 `thinking_level`·`temperature`·`model`, `cache.{enabled,ttl}`, `pipeline.max_return_loops`, `pipeline.max_concurrency`, `pipeline.expand_multi_dependent`(다중 종속 인용을 대안 체인별로 역구성), `lessons.inject`, `telemetry.pricing`(비용 추정용 $/M). 실험 변형은 점 표기 키로 이를 덮어쓴다.

스타일 조정자의 조건부 코퍼스 검색은 2단계 호출로 구현된다(`aux_mode: two_phase`): 1단계는 도구(`open_routing_index`, `search_style_corpus`) 가능 텍스트 모드, 2단계는 도구 로그를 첨부한 JSON 모드. 도구는 정확 조각 최대 2개만 반환하고 분야·아키텍처 검색을 거부한다. `off`로 끄면 `NOT_ACTIVATED`로 기록된다.

## 개선 루프

1. **피드백 파이프라인** — `claim-copa feedback [--since YYYY-MM-DD]`: 모든 `telemetry.jsonl`을 집계해 역할별 토큰·지연, 비-PASS 게이트×사유, 세부 시험 실패 상위, RETURN_TO_* 흐름, 패턴 카드(2회 이상 반복)를 `reports/feedback-<date>.md`로 만든다. 텔레메트리는 청구항 원문·원자료를 담지 않는다.
2. **실험** — `experiments/<name>.yaml`(overrides, prompt_suffix, lessons 집합)과 `eval/cases/<case>/{request.yaml, expected.yaml, sources/, fixtures/}`.
   ```bash
   claim-copa eval run --case sample-clip-holder --variant experiments/example-medium-thinking.yaml --baseline --shadow
   claim-copa eval run --all --record fixtures/live        # 라이브 응답 녹화 → 이후 --replay
   claim-copa run ... --shadow experiments/x.yaml          # 같은 패킷으로 변형을 병행 호출, runs/<id>/shadow/에 기록만
   ```
   `expected.yaml`은 단계별 허용 게이트 집합(`OA.OA_FINAL_GATE: [UNVERIFIED]`), outcome, must_contain/must_not_contain, max_loops를 검사한다. shadow는 단계 단위 비교이며 상태·LOCK에 영향을 주지 않는다.
3. **지속 학습(승인제 교훈)** — `lessons/{pending,approved,rejected}/L-NNNN.yaml`.
   ```bash
   claim-copa lessons propose --from-run <run_id>     # 중지 쟁점에서 초안(pending)
   claim-copa lessons propose --text "…" --roles claim-style-adjuster --rationale "…"
   claim-copa lessons approve L-0001 && claim-copa lessons list
   ```
   승인된 교훈만 해당 역할의 system instruction 끝에 "절차·표현 주의사항(기술내용 근거 아님)"으로 주입되며, 그 digest가 `source_set_id`에 포함되어 승인 즉시 이후 기록의 소스 집합이 바뀐다.

## 라이브 스모크 절차 (사용자 PC)

1. `export GEMINI_API_KEY=…` 후 `claim-copa doctor --live` — 모델 존재, JSON+thinking_level, 캐시 생성 프로브 확인.
2. `claim-copa.yaml`의 `model.default`를 `claim-copa models` 결과의 실제 ID로 수정.
3. `claim-copa run --request-yaml eval/cases/sample-clip-holder/request.yaml --record fixtures/live --run-id live-01`.
4. 통과하면 `claim-copa fixtures sanitize fixtures/live`로 요청 미리보기를 지우고 `eval/cases/sample-clip-holder/fixtures/`를 교체한다(현재 fixtures는 `scripts/make_sample_fixtures.py`가 만든 합성 응답이다).

## 테스트

```bash
python -m pytest            # 33 tests: 상태기계(scripted), 무효화·루프 한도·resume, 블라인드 격리, 코퍼스, 교훈, 피드백, Gemini provider 모킹
```
`tests/scripted_roles.py`의 canned 봉투가 각 역할의 PASS/RETURN/REVIEW 응답을 흉내 낸다. 실제 Gemini 응답은 `--record`로 녹화한 fixture로 대체한다.
