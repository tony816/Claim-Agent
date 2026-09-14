# Claim-Agent Python + Gemini 런타임

> 처음 쓰는 분을 위한 안내는 [`../사용법.md`](../사용법.md)에 있다. 이 문서는 설정과 내부 구조를 다룬다.

`claim_agent`는 `CLAUDE.md`의 19단계 다중 에이전트 절차를 Claude Code 없이 실행하는 독립 프로그램이다. 역할 프롬프트는 계속 `.claude/agents/*.md`가 단일 출처이며(frontmatter만 제거해 system instruction으로 사용), 소스 권한은 `sources/README.md`를 그대로 따른다. 실행 프로필은 `PY_GEMINI_MULTI_CALL`이고 LOCK의 `lock_class`는 `DRAFT-ISOLATED-PY`다.

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

웹챗의 모든 전송은 수동 모드 선택과 관계없이 요청 분류를 거친다. 선택값과 종속항 옵션은 `ui_hints`로만 전달하며, 현재 질문이 요구하는 산출물이 우선한다. “변경하라는 지시가 있는데 어느 방법이 적절한가”라는 질문은 수정본 생성과 구분하여 `REVIEW_ONLY`로 처리한다. 작성 게이트에서 중지된 경우에도 그 역할의 실제 분석 보고서를 결과 화면에 표시하되 후속 미검수 상태를 명시한다.

```bash
pip install -e .            # google-genai, pydantic, pyyaml, python-dotenv
# 프로젝트 루트의 .env에 GEMINI_API_KEY=... 저장 (없으면 --replay만 가능)
claim-agent doctor --contracts      # 키·소스·역할·스키마 점검 (+ --live로 모델·JSON·캐시 프로브)
claim-agent models                  # 실제 사용 가능한 모델 ID 확인 → claim-agent.yaml의 model.default 수정
```

기본 모델 ID `gemini-3.8-flash`는 설정값일 뿐 확인된 값이 아니다. `claim-agent models`로 목록을 보고 `claim-agent.yaml`을 고친다. `thinking_level`을 지원하지 않는 모델이면 provider가 자동으로 `thinking_budget`으로 폴백하고, 캐시 최소 토큰 미달이면 인라인 전송으로 폴백한다.

## 실행

Windows에서는 `truststore`로 운영체제의 신뢰 인증서를 사용하며 TLS 인증서·호스트명 검증을 유지한다.

### 기본 웹 채팅

Windows 더블클릭 실행기는 `scripts/launch_web.py`를 통해 `claim_agent.web`의 로컬 HTTP 서버를 백그라운드로 열고 기본 브라우저를 실행한다. 서버가 살아 있으면 재사용한다. 직접 실행은 `python -m claim_agent.web`이며 `--project-root`, `--config`, `--no-browser`를 지원한다. 정적 화면은 웹 모듈과 함께 설치되는 `web_assets/`에 있고, 브라우저 기본 textarea의 IME 조합 이벤트를 보호한다.

서버는 `127.0.0.1`의 임의 포트에만 바인딩한다. 실행기 토큰으로 HttpOnly/SameSite 쿠키를 설정하고 API는 Host·Origin·요청 헤더를 검증한다. API 키는 서버 환경변수에만 유지한다. 업로드는 세션별 복사본과 ID로 관리하고 `.env`, 경로 탈출, 지원하지 않는 형식 및 20MB 초과 파일을 거부한다. API에서 임의 로컬 경로를 읽지 않는다.

대화 목록·메시지·Gemini 역할 이력은 `.tui/web/sessions/`에 저장된다. 기본 선택인 **자동 분류 · 대화**는 `claim_agent.chat`에서 답변 전에 현재 `CLAUDE.md`와 이전 문맥으로 요청을 분류한다. 청구항 출력물·수정안은 `AUTHORING_DRAFT`, 명시적 출원용 최종 확정은 `FINALIZATION`, 특허 의견은 `REVIEW_ONLY`로 기존 `PipelineEngine`에 연결된다. 작성·수정은 필수 역할과 게이트 순서를 거치며, 제한 검수는 요청된 리뷰어만 호출한다. 수동 **청구항 작성·수정** 선택도 유지된다.

`routing.py`는 분류 JSON과 입력 원문 ID를 검증하고, `conversation_pipeline.py`는 사용자 원자료와 모델 참고 답변을 분리해 전달한다. 분류 실패·필수 입력 부족·게이트 실패를 일반 대화로 우회하지 않는다. 후속 수정은 같은 run/candidate와 USER_LOCK을 보존한 `ARCHITECT` 재설계로 처리하고 revision·design_revision을 올린다. 종속항도 요청 내용에서 판별한다. REVIEW_ONLY는 실제 리뷰어 보고서를 표시하며 LOCK을 발급하지 않는다.

화면에는 자동 분류 결과가 표시된다. `.tui/requests/<id>/route.json`에 분류 결정을, `runs/<run_id>/calls`, `records`, `state.json`, `report.md`에 실제 실행·게이트·미검증 결과를 저장한다. 분류는 모델 판단이므로 오분류 가능성이 있고, 명시적인 특허 요청이 일반 대화로 분류되면 추가 코드 검사로 중지한다. 브라우저는 모델의 역할별 JSON을 답변 본문에 섞지 않고 작업 로그에만 표시한다. 종료 버튼은 자식 프로세스와 서버를 종료한다. 탭 종료는 실행을 중단하지 않는다.

검증: `python -m pytest tests/test_web.py -q`. 선택적 브라우저 검사는 `tests/web_browser_server.py <메타데이터 경로>`와 `tests/web_browser.cjs <같은 경로>`를 사용한다. Node.js·Playwright·Edge가 필요하며, 해당 서버는 실제 API 대신 오프라인 응답만 생성한다.

### 이전 터미널 UI (선택)

CLI에서 `python -m claim_agent.cli tui`로 연다. `--project-root`, `--config`는 기존 전역 옵션이며 TUI도 같은 설정과 `.env`를 사용한다. `Claim-Agent.cmd`의 기본 실행 대상은 이제 웹 채팅이다.

TUI는 Textual 기반으로, 원자료를 `.tui/requests/<id>/`에 저장하고 기존 CLI를 `shell=False`인 별도 프로세스로 실행한다. 요청과 원자료는 별도 필드이며 기존 단계·검수·LOCK 로직을 그대로 사용한다. `state.json`에서 저장된 단계와 record 상태를 읽어 표시하고 완료 보고서를 결과 영역에 연다. 중지는 자식 프로세스를 종료한다. 네이티브 파일 선택과 클립보드는 별도 프로세스에서 처리해 화면 입력을 막지 않는다.

기본 모드는 단순 대화이며 `claim_agent.chat`가 별도 프로세스에서 역할 구분이 보존된 이전 메시지와 현재 입력으로 Gemini를 호출한다. 대화에는 청구항 파이프라인을 적용하지 않는다. 각 턴의 `chat.json`, `response.json`, `report.md`를 `.tui/requests/<id>/`에 보관한다. 화면의 대화 맥락은 현재 창에서 유지되며 재실행 시 자동 복원되지는 않는다.

프롬프트 창은 대화·요청·피드백에 공통으로 사용한다. FEEDBACK 모드는 저장된 기존 run을 선택해 `resume <run_id> --decision-file ... --restart-from ARCHITECT`를 실행한다. 기존 원자료와 USER_LOCK을 보존하며 독립항 및 종속항의 이전 LOCK을 무효화하고 새로운 설계·문언 revision의 필수 게이트를 재실행한다. 추가 원자료는 `--add-source`로 전달한다. 피드백별 이벤트 로그와 입력은 별도의 `.tui/requests/<새 ID>/`에 저장하고, 보고서·state는 기존 run 디렉터리에 유지한다. 수정 보고서가 새로 생성되지 않으면 이전 보고서를 수정 완료 결과로 표시하지 않는다.

TUI 자식 프로세스에만 `CLAIM_AGENT_EVENT_LOG` 경로를 전달한다. `EventWriter`는 API 키를 가리고 요청·응답 이벤트를 JSONL로 즉시 기록한다. GeminiProvider는 이벤트 기록이 활성화되고 도구가 없는 호출을 스트리밍으로 처리해 최종 출력 JSON을 다시 조립한다. 도구 호출은 기존 SDK 실행 경로를 유지하고 완료 후 응답과 도구 결과를 기록한다. `thought` 표시가 있는 모델 내부 사고 부분은 중계하지 않는다. TUI는 이벤트 파일을 0.2초 간격으로 읽으며 응답 후 로그를 자동으로 접고 펼치기 버튼으로 다시 연다. 로그는 역구성 입력이나 다른 에이전트의 패킷에 주입하지 않는다.

파일 선택, 터미널 경로 드롭, 탐색기 파일 복사, 클립보드 이미지, 일반 텍스트 붙여넣기를 지원한다. 파일 드롭 자체는 터미널 호스트가 경로로 전달해야 하며 파일 선택 버튼을 대체 수단으로 제공한다. 구체적인 조작은 `사용법.md`의 첫 절을 참조한다. UI 구현 참고: [Textual TextArea](https://textual.textualize.io/widgets/text_area/), [Workers](https://textual.textualize.io/guide/workers/).

```bash
# 잠정 독립항 + 잠정 종속항 세트 (AUTHORING_DRAFT)
claim-agent run --mode AUTHORING_DRAFT --request-file req.md \
  --source 발명설명.md --drawing 도1.png --prior-art NONE --dependent --dependent-target "2~8"

# request.yaml 하나로 실행 (eval/cases/*/request.yaml 형식)
claim-agent run --request-yaml eval/cases/sample-clip-holder/request.yaml

# 오프라인 리플레이 (API 키 없이 파이프라인 동작 확인)
claim-agent run --request-yaml eval/cases/sample-clip-holder/request.yaml \
  --replay eval/cases/sample-clip-holder/fixtures

# 청구항만 출력
claim-agent runs report <run_id> --claims-only
```

결과는 `runs/<run_id>/`에 남는다: `state.json`, `calls/NNN-<role>-<scope>.{json,md}`(패킷·응답·봉투·보고서 전문), `records/<record_id>.{json,md}`(LOCK 포함), `telemetry.jsonl`, `report.md`.

종료 코드: 0 LOCK 도달 / 2 REVIEW 중지 / 3 BLOCK·UNVERIFIED 중지 / 4 루프 한도 / 5 봉투 무효 / 1 오류.

### 중지와 재개

역할이 라우팅 불가한 REVIEW/BLOCK/UNVERIFIED를 반환하면 즉시 중지하고 `report.md`의 `남은 REVIEW/BLOCK/UNVERIFIED`에 쟁점과 재개 명령을 적는다. 사용자가 결정의 성격을 분류한다.

```bash
claim-agent resume <run_id> --decide "오목면의 주체는 기둥 둘레면"          # 같은 단계 재실행(결정 문구를 패킷에 첨부)
claim-agent resume <run_id> --decide "길이 방향으로 띄어쓰기" --apply-style-fix   # STYLE_ONLY_REVISION, r+1
claim-agent resume <run_id> --decide "각각의 분배 원천 명시" --apply-meaning-fix # DRAFTER_REVISION, r+1
claim-agent resume <run_id> --decide "…" --redesign                            # design_revision d+1, architect부터
claim-agent resume <run_id> --add-source 추가도면.png                           # input_revision 변경 → d+1
claim-agent resume <run_id> --accept-unverified                               # 비게이팅 UNVERIFIED만 허용
```

`RETURN_TO_*`는 자동으로 처리된다(STYLE_ONLY 2회, DRAFTER 2회, ARCHITECT 1회, DEPENDENT_ARCHITECT 1회 기본). 한도를 넘으면 `LOOP_LIMIT`로 중지한다. `OA_FINAL_GATE: UNVERIFIED — SPEC_NOT_PROVIDED`는 AUTHORING_DRAFT의 역구성을 막지 않는다(하드 규칙이 코드에 있음). 소스·역할 파일·승인 교훈이 바뀌면 `source_set_id`가 바뀌고 이전 run은 STALE로 표시되어 `--restart-from ARCHITECT` 없이는 재개할 수 없다.

### REVIEW_ONLY

```bash
claim-agent review --reviewers syntax-scope-reviewer,oa-strategy-reviewer --claim-file claims.md --scope INDEPENDENT
```
`r1`, `design_revision: N/A`로 요청된 검수자만 호출하며 LOCK을 만들지 않는다.

## 설정 (`claim-agent.yaml`)

역할별 `thinking_level`·`temperature`·`model`, `cache.{enabled,ttl}`, `pipeline.max_return_loops`, `pipeline.max_concurrency`, `pipeline.expand_multi_dependent`(다중 종속 인용을 대안 체인별로 역구성), `lessons.inject`, `telemetry.pricing`(비용 추정용 $/M). 실험 변형은 점 표기 키로 이를 덮어쓴다.

스타일 조정자의 조건부 코퍼스 검색은 2단계 호출로 구현된다(`aux_mode: two_phase`): 1단계는 도구(`open_routing_index`, `search_style_corpus`) 가능 텍스트 모드, 2단계는 도구 로그를 첨부한 JSON 모드. 도구는 정확 조각 최대 2개만 반환하고 분야·아키텍처 검색을 거부한다. `off`로 끄면 `NOT_ACTIVATED`로 기록된다.

## 개선 루프

1. **피드백 파이프라인** — `claim-agent feedback [--since YYYY-MM-DD] [--window 10] [--threshold 0.15]`: 모든 `telemetry.jsonl`을 집계해 역할별 토큰·지연, 비-PASS 게이트×사유, 세부 시험 실패 상위, RETURN_TO_* 흐름, 패턴 카드(2회 이상 반복), 그리고 **선제 경고**를 `reports/feedback-<date>.md`로 만든다. LLM은 쓰지 않는다.
   - 선제 경고는 최근 `--window` run과 이전 구간을 비교해 치명적 실패 전에 리스크를 표면화한다. `DRIFT_WARNING`(역할별 비-PASS율·출력 복구율이 임계만큼 상승, 지연·출력 토큰이 1.5배 이상), `SPIKE`(같은 역할×게이트×사유가 3 run 연속, 또는 루프 한도·봉투 위반 중지가 최근 창에 2회 이상), `TOOL_ANOMALY`(코퍼스 도구의 금지 질의 거부, 무결과 과반, 재검토 대상 조각 사용).
   - 정식 명세서·선행기술 미제공에 따른 `UNVERIFIED`는 CLAUDE.md가 DRAFT 게이트 PASS와 공존을 허용한 비게이팅 상태이므로 실패로 세지 않고 별도 카운트만 남긴다. 이걸 실패로 세면 모든 AUTHORING_DRAFT run이 실패로 보여 신호가 묻힌다.
   - 텔레메트리는 청구항 원문·원자료를 담지 않는다. 도구 호출도 `phase="tool"` 행으로 남지만 질의 원문 대신 길이·결과 수·거부 여부만 기록한다.

   **RCA — `claim-agent runs rca <run_id> [--out PATH]`**: 한 run의 근본 원인 분석을 `runs/<run_id>/rca.md`로 만든다. 네 단계다.
   - 워크플로 추적: 호출 순서대로 단계·역할·record_id·상태·비-PASS 게이트·토큰·지연, 그리고 되돌아간 지점(`RETURN_TO_*`, revision·design_revision bump, 폐기된 revision)
   - 결함 국소화: 첫 실패 지점의 게이트·사유·실패한 세부 시험·미해결 쟁점과 바로 직전에 통과한 단계
   - 패턴 인식: 피드백 패턴 카드와 대조해 `고립 사건` 또는 `반복 추세(N회, 예시 run)`
   - 영향 평가: 심각도(BLOCK·루프 한도·봉투 위반 3 / REVIEW 2 / UNVERIFIED 1) × 반복 횟수로 우선순위 점수, 재작업으로 버려진 토큰, 되돌아갈 단계와 정확한 재개 명령
2. **실험** — `experiments/<name>.yaml`(overrides, prompt_suffix, lessons 집합)과 `eval/cases/<case>/{request.yaml, expected.yaml, sources/, fixtures/}`.
   ```bash
   claim-agent eval run --case sample-clip-holder --variant experiments/example-medium-thinking.yaml --baseline --shadow
   claim-agent eval run --all --record fixtures/live        # 라이브 응답 녹화 → 이후 --replay
   claim-agent run ... --shadow experiments/x.yaml          # 같은 패킷으로 변형을 병행 호출, runs/<id>/shadow/에 기록만
   ```
   `expected.yaml`은 단계별 허용 게이트 집합(`OA.OA_FINAL_GATE: [UNVERIFIED]`), outcome, must_contain/must_not_contain, max_loops를 검사한다. shadow는 단계 단위 비교이며 상태·LOCK에 영향을 주지 않는다.
3. **지속 학습(승인제 교훈·역전파)** — `lessons/{pending,approved,rejected}/L-NNNN.yaml`.
   ```bash
   claim-agent lessons propose --from-feedback [--min-count 2]   # 반복 패턴 → 역할별 교훈 초안(역전파)
   claim-agent lessons propose --from-run <run_id>               # 중지 쟁점에서 초안
   claim-agent lessons propose --from-run <run_id> --llm         # 모델이 초안 작성 (opt-in)
   claim-agent lessons propose --text "…" --roles claim-style-adjuster --rationale "…"
   claim-agent lessons approve L-0001 && claim-agent lessons list
   ```
   텍스트 피드백을 프롬프트로 되돌리는 경로가 `--from-feedback`이다. 패턴 카드마다 대상 역할·게이트·자주 실패한 시험을 담은 초안을 만들고, 같은 `(역할, 게이트, 사유)` 서명이 이미 있으면 새 교훈 대신 근거 run만 덧붙인다.

   `--llm`은 선택이다. 모델에 보내는 것은 중지 지점의 게이트·실패 시험·미해결 쟁점·최소 수정 방향뿐이고 보고서 전문이나 청구항 문언은 보내지 않으며, 프롬프트가 기술내용 인용을 금지한다. 결과는 항상 `pending`이고 `llm_drafted: true`로 표시된다.

   승인된 교훈만 해당 역할의 system instruction 끝에 "절차·표현 주의사항(기술내용 근거 아님)"으로 주입되며, 그 digest가 `source_set_id`에 포함되어 승인 즉시 이후 기록의 소스 집합이 바뀐다.

   > 자동 파이프라인은 패턴을 찾고 변경을 제안할 뿐 문맥적 뉘앙스나 전략적 우선순위를 판단하지 못한다. 모든 제안은 사람이 검토·검증하고 필요하면 무시해야 하며, 승인 전에는 어떤 교훈도 역할 프롬프트에 들어가지 않는다.

## 라이브 스모크 절차 (사용자 PC)

1. 프로젝트 루트의 `.env`에 `GEMINI_API_KEY=…` 저장 후 `claim-agent doctor --live` — 모델 존재, JSON+thinking_level, 캐시 생성 프로브 확인. 같은 이름의 기존 환경변수가 있으면 그 값이 우선한다.
2. `claim-agent.yaml`의 `model.default`를 `claim-agent models` 결과의 실제 ID로 수정.
3. `claim-agent run --request-yaml eval/cases/sample-clip-holder/request.yaml --record fixtures/live --run-id live-01`.
4. 통과하면 `claim-agent fixtures sanitize fixtures/live`로 요청 미리보기를 지우고 `eval/cases/sample-clip-holder/fixtures/`를 교체한다(현재 fixtures는 `scripts/make_sample_fixtures.py`가 만든 합성 응답이다).

## 테스트

```bash
python -m pytest            # 44 tests: 상태기계(scripted), 무효화·루프 한도·resume, 블라인드 격리, 코퍼스, 교훈, 피드백, RCA·선제 경고·역전파·도구 텔레메트리, Gemini provider 모킹
```
`tests/scripted_roles.py`의 canned 봉투가 각 역할의 PASS/RETURN/REVIEW 응답을 흉내 낸다. 실제 Gemini 응답은 `--record`로 녹화한 fixture로 대체한다.

## 반복 컨텍스트 전달 최적화

종속항별 도면 비교는 루트 LOCK·설계·종속항 설계·스타일·OA 보고서와 원자료·도면을 공통 캐시에 저장한다. 각 호출에는 부모항 체인, 목표항, 독립 blind snapshot 및 해당 호출 식별자만 별도로 보낸다. 캐시 키는 역할·scope·모델·system instruction·공통 텍스트·도면 내용/MIME/라벨·run_id에 결속된다. 보고서의 revision과 원자료가 바뀌면 키도 바뀐다. 동일 프로세스의 병렬 목표항은 하나의 캐시 생성 결과를 공유한다.

캐시가 실패하거나 만료되면 기존 전체 입력으로 복구한다. blind 호출은 캐시 대상에서 제외한다. 로컬 calls의 packet_text에는 인계 전문을 그대로 남기며 context_transport에 공통 본문 길이와 이미지 개수를 기록한다. 캐시는 반복 전송·처리 비용을 줄이는 방법이며, 모델이 받는 전체 문맥 길이나 추론 시간이 같은 비율로 줄어든다는 뜻은 아니다.

숫자로 지정된 종속항 범위는 설계 후보, PRE_STYLE, 최종 스타일 세트에서 결정론적으로 대조한다. 범위 확대·누락·번호 중복은 REQUEST_SCOPE_MISMATCH로 중지하며 이전 PASS나 LOCK으로 우회하지 않는다. UI 옵션보다 현재 질문의 항 번호가 우선한다.

API 전달 방식 참고: [Gemini context caching](https://ai.google.dev/gemini-api/docs/caching).
