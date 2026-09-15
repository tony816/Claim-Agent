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
pip install -e .            # google-genai, pydantic, pyyaml, python-dotenv, Pillow, pypdf, python-docx, olefile (+ anthropic: provider.kind=anthropic일 때)
# 프로젝트 루트의 .env에 GEMINI_API_KEY=... 저장 (없으면 --replay만 가능). Anthropic은 ANTHROPIC_API_KEY.
claim-agent doctor --contracts      # 키·소스·역할·스키마 점검 (+ --live로 모델·JSON·캐시 프로브, 단가 누락 경고)
claim-agent models                  # 실제 사용 가능한 모델 ID 확인 → claim-agent.yaml의 model.default 수정
```

역할 파일의 파생본(`.codex/agents/*.toml`, 웹 번들)은 `python scripts/build_role_derivatives.py`로 `.claude/agents/*.md`에서 생성하며 CI가 `--check`로 드리프트를 막는다. 역할 프롬프트를 고치면 이 스크립트를 한 번 실행한다.

기본 모델 ID `gemini-3.8-flash`는 설정값일 뿐 확인된 값이 아니다. `claim-agent models`로 목록을 보고 `claim-agent.yaml`을 고친다. `thinking_level`을 지원하지 않는 모델이면 provider가 자동으로 `thinking_budget`으로 폴백하고, 캐시 최소 토큰 미달이면 인라인 전송으로 폴백한다.

## 실행

Windows에서는 `truststore`로 운영체제의 신뢰 인증서를 사용하며 TLS 인증서·호스트명 검증을 유지한다.

### 기본 웹 채팅

Windows 더블클릭 실행기는 `scripts/launch_web.py`를 통해 `claim_agent.web`의 로컬 HTTP 서버를 백그라운드로 열고 기본 브라우저를 실행한다. 서버가 살아 있으면 재사용한다. 직접 실행은 `python -m claim_agent.web`이며 `--project-root`, `--config`, `--no-browser`를 지원한다. 정적 화면은 웹 모듈과 함께 설치되는 `web_assets/`에 있고, 브라우저 기본 textarea의 IME 조합 이벤트를 보호한다.

서버는 `127.0.0.1`의 임의 포트에만 바인딩한다. 실행기 토큰으로 HttpOnly/SameSite 쿠키를 설정하고 API는 Host·Origin·요청 헤더를 검증한다. API 키는 서버 환경변수에만 유지한다. 업로드는 세션별 복사본과 ID로 관리하고 `.env`, 경로 탈출, 지원하지 않는 형식 및 20MB 초과 파일을 거부한다. API에서 임의 로컬 경로를 읽지 않는다.

대화 목록·메시지·Gemini 역할 이력은 `.tui/web/sessions/`에 저장된다. 기본 선택인 **자동 분류 · 대화**는 `claim_agent.chat`에서 답변 전에 현재 `CLAUDE.md`와 이전 문맥으로 요청을 분류한다. 청구항 출력물·수정안은 `AUTHORING_DRAFT`, 명시적 출원용 최종 확정은 `FINALIZATION`, 특허 의견은 `REVIEW_ONLY`로 기존 `PipelineEngine`에 연결된다. 작성·수정은 필수 역할과 게이트 순서를 거치며, 제한 검수는 요청된 리뷰어만 호출한다. 수동 **청구항 작성·수정** 선택도 유지된다.

**프로젝트 폴더.** `.tui/web/projects/<id>/project.json`은 이름·설명·`instructions`·`user_lock`·소스 파일 목록(종류: invention/drawing/prior_art/spec)을 저장하고, 파일 본문은 `files/<file_id>/<원본 이름>`에 둔다. 세션은 `project_id`로 프로젝트에 종속되며 `/api/new`에 `project_id`를 넘겨 만든다. `/api/projects`·`/api/project`·`/api/project/new|update|delete|upload|remove-file`가 관리 API다. 프로젝트 세션의 매 전송에서 `Workspace.project_inputs`가 `chat.json`에 `instructions`, `user_lock`, 프로젝트 파일(`files` 앞부분과 `attachments`의 종류·`project_file_id`)을 채운다. 지침은 라우터 패킷의 `project_instructions`, 일반 대화의 system instruction 뒤 프로젝트 지침 블록, 파이프라인의 `현재 요청` 앞 `## 프로젝트 지침` 절로만 전달되며 원자료 블록이나 MaterialBundle에는 들어가지 않는다. `user_lock`은 새 run의 `RunRequest.user_lock`이 되고, 기존 run의 후속 수정은 run에 저장된 USER_LOCK을 유지한다. `revision_path`는 `project_file_id`가 붙은 프리셋 파일을 이번 턴의 새 첨부로 보지 않으므로 스타일·의미 수정 경로가 그대로 열린다. 프로젝트 삭제는 폴더만 지우고 세션의 `project_id`를 해제한다.

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

역할별 `thinking_level`·`temperature`·`model`, `cache.{enabled,ttl,warm,min_expected_reuse}`, `pipeline.max_return_loops`, `pipeline.max_concurrency`, `pipeline.expand_multi_dependent`(다중 종속 인용을 대안 체인별로 역구성; `resume --expand-multi`로 그때그때 승인 가능), `pipeline.{max_calls,max_total_tokens,max_cost_usd}`(예산 가드), `materials.{max_image_side,files_api}`, `retention.{days,keep_locks}`, `provider.kind`, `lessons.inject`, `telemetry.pricing`(비용 추정용 $/M; 예시 값은 실제 단가표로 확인). 실험 변형은 점 표기 키로 이를 덮어쓴다.

스타일 조정자의 조건부 코퍼스 검색은 2단계 호출로 구현된다(`aux_mode: two_phase`): 1단계는 도구(`open_routing_index`, `search_style_corpus`) 가능 텍스트 모드로, README·07만 사전 로딩하고 도면을 제외하며 `aux_thinking_level`(기본 LOW)·`aux_max_output_tokens`(기본 2048)로 가볍게 실행한다. 2단계는 도구 로그를 첨부한 JSON 모드다. 도구는 정확 조각 최대 2개만 반환하고 분야·아키텍처 검색을 거부한다. `off`로 끄면 `NOT_ACTIVATED`로 기록된다.

## 프로바이더

`provider.kind`가 실제 호출을 담당한다. 역할 파일·패킷·게이트·기록은 프로바이더 중립이며 fixtures/replay도 공통이다.

| | `gemini` (기본) | `anthropic` |
|---|---|---|
| 클라이언트 | `google-genai`, `GEMINI_API_KEY` | `anthropic`, `ANTHROPIC_API_KEY`(또는 `ant auth login` 프로필) |
| 모델 | `model.default`, 역할별 `model` | `provider.anthropic.model`(기본 `claude-opus-5`), 역할별 `model` |
| 캐시 | 명시적 context cache(아래 정책) + Files API | `cache_control`(프롬프트 접두 캐시)을 시스템 지시·소스 블록에 표시 |
| 사고 | `thinking_level` → `thinking_level`/`thinking_budget` | adaptive thinking + `output_config.effort`(HIGH→high 등); `temperature`는 보내지 않음 |
| JSON | `response_json_schema` | `output_config.format`(구조화 출력); 거부되면 프롬프트 JSON 지시로 재시도 |
| 도구 | SDK 자동 함수 호출 | 수동 tool_use→tool_result 루프(최대 6회) |
| 거부 | — | `stop_reason: refusal`이면 ProviderError; `fallbacks: default`(베타)로 서버 측 대체 모델을 먼저 시도 |

`claim-agent models`·`doctor --live`는 설정된 프로바이더로 동작한다. 웹·TUI 대화 경로도 같은 프로바이더(`CallSpec.history`로 이전 턴 전달)를 쓴다.

## 입력 형식과 도면

텍스트 원자료는 `.md/.txt/.yaml/.json/.csv` 외에 `.pdf`(텍스트 레이어), `.docx`, `.hwpx`, `.hwp`(HWP 5.0)를 `sources/extract.py`가 본문 텍스트로 추출한다. 스캔 PDF·암호화 문서·배포용 HWP는 업로드 시점에 명확한 사유로 거부한다(OCR 미지원). 원본 파일의 sha256이 원자료 식별자이며 추출 요약은 `material_meta.extraction`에 남는다.

도면은 intake에서 한 번 정규화한다(`materials.max_image_side`, 기본 2048px, 도면 부호 판독을 위해 더 줄이지 않음). 원본·정규화본 sha가 모두 기록되어 `input_revision`에 반영된다. `materials.files_api`가 켜져 있으면 Gemini Files API에 콘텐츠 해시당 1회 업로드하고 이후 호출은 URI로 참조한다(레지스트리 `runs/.files-registry.json`, 보존 기간 내 프로세스 간 재사용, 실패 시 inline 폴백). 호출 기록의 `context_transport.image_transport`로 확인한다.

## 예산 가드와 성능 요약

`pipeline.max_calls`·`max_total_tokens`·`max_cost_usd`(또는 `run/resume --max-calls …`)를 넘으면 다음 호출 전에 `HALTED_BUDGET_LIMIT`(종료 코드 4)로 중지한다. 완료된 호출은 버리지 않으며, 한도를 올린 뒤 `claim-agent resume <run_id>`로 이어서 진행한다. `report.md`의 **성능 요약**은 단계별 호출·지연·입력/캐시/출력 토큰·캐시 적중·추정 비용을 표로 보여 주고, CLI 요약도 한 줄을 출력한다. 비용은 `telemetry.pricing`에 사용 모델이 있어야 산정된다.

## 후속 수정의 revision 경로

웹·대화에서 기존 run을 이어 수정하면 라우터가 `revision_kind`(STYLE_ONLY·MEANING·DESIGN)를 고르고, `conversation_pipeline.revision_path`가 결정론적으로 검증한다. 새 자료·도면·선행기술·명세서·첨부가 있으면 항상 architect 재설계(d+1)이고, 그렇지 않으면 스타일만(r+1, style adjuster부터) 또는 의미(r+1, drafter부터)로 같은 run을 재개한다. 요청이 종속항 번호만 지목하고 유효한 종속항 세트가 있으면 종속항 revision만 올린다. CLI는 `resume --apply-style-fix|--apply-meaning-fix|--redesign [--scope DEPENDENT]`가 같은 경로다. 웹의 작업 상태 패널에서 결정·종류·범위·추가 자료를 골라 재개할 수 있다.

## 내보내기·리비전 대조·보관

- `claim-agent export <run_id> --format docx|md [--with-evidence]`: 잠긴 exact 문언을 헤더(LOCK 종류·식별자·sha256·잠정/최종 표시)와 함께 내보내고, DOCX는 다시 읽어 문언이 보존되었는지 검증한다. 웹은 `/api/export`.
- `claim-agent runs diff <run_id> [--from r1 --to r2] [--scope DEPENDENT_SET]`: `기존 문언 / 제안 문언 / 문장 단위 변경 / 변경 이유 / 권리범위 영향(style record 인용) / 근거`. `report.md`에는 리비전 이력 표가 붙는다.
- `claim-agent runs purge --older-than N [--include-locks] [--dry-run]`: `runs/`와 `.tui/requests/`에는 원자료·패킷 전문이 평문으로 남으므로 주기적으로 지운다. LOCK에 도달한 run은 기본 보존. `retention.days`로 기본값을 둔다.
- 역할 봉투의 `limitation_evidence[]`(한정별 근거표)는 기록·내보내기 부록에 남고, `UNCONFIRMED` 행이 PASS와 함께 오면 오케스트레이터가 REVIEW로 중지한다(조건 4 근거표 공란 0건).

## 골든 평가 세트

프롬프트·모델·엔진 변경이 개선인지 판정하는 유일한 기준 데이터다. `eval/cases/README.md`에 구조와 `expected.yaml` 필드(주골격 용어·금지 용어·USER_LOCK 보존·발명 유형·역구성 판정·호출 상한)가 있다. `claim-agent eval new <case>`로 스캐폴드하고, `claim-agent eval live --case <case> --record …`로 예상 호출·비용을 확인한 뒤 1회 녹화하며, 이후 CI는 `eval run --all --replay-only`로 리플레이한다. 기밀 자료는 `eval/private/`(gitignore)에 두고 sanitize한 fixtures만 커밋한다.

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

1. 프로젝트 루트의 `.env`에 `GEMINI_API_KEY=…` 저장 후 `claim-agent doctor --live` — 모델 존재, JSON+thinking_level, 캐시 생성 프로브, 단가 등록 여부 확인. 같은 이름의 기존 환경변수가 있으면 그 값이 우선한다.
2. `claim-agent.yaml`의 `model.default`를 `claim-agent models` 결과의 실제 ID로 수정하고 `telemetry.pricing`에 그 모델의 단가를 넣는다.
3. `claim-agent eval live --case sample-clip-holder --record eval/cases/sample-clip-holder/fixtures` — 예상 호출 수·비용을 확인하고 실제로 1회 녹화한다.
4. 통과하면 `claim-agent fixtures sanitize eval/cases/sample-clip-holder/fixtures`로 요청 미리보기를 지운다(현재 커밋된 fixtures는 `scripts/make_sample_fixtures.py`가 만든 합성 응답이며 엔진 회귀용이다). 실제 발명 케이스는 `claim-agent eval new`로 추가한다.

## 테스트

```bash
python -m pytest            # 전체 테스트: 상태기계(scripted), 무효화·루프 한도·resume, 블라인드 격리, 코퍼스, 교훈, 피드백, RCA·선제 경고·역전파·도구 텔레메트리, 캐시 정책·전송, 예산, 파서, 문서 추출, 내보내기, diff·보관, 웹 API, Gemini·Anthropic provider 모킹
ruff check claim_agent tests scripts && python scripts/build_role_derivatives.py --check && claim-agent eval run --all --replay-only   # CI가 실행하는 나머지
```
`tests/scripted_roles.py`의 canned 봉투가 각 역할의 PASS/RETURN/REVIEW 응답을 흉내 낸다. 실제 모델 응답은 `--record`로 녹화한 fixture로 대체한다. `mypy claim_agent`는 CI에서 권고(advisory)로 돌며 오류 수가 0이 되면 필수로 바꾼다.

## 반복 컨텍스트 전달 최적화

**캐시 생성 정책.** Gemini의 명시적 context cache는 생성 시 일반 입력 단가, 저장 시간 과금, 적중 시 할인이므로 한 번만 쓰는 번들에는 손해다. `cache.warm: auto`(기본)는 같은 역할×scope 번들을 이번 run에서 `min_expected_reuse`회 이상 쓰는 경우(스타일 2단계, 종속항 picture 팬아웃)이거나 같은 번들이 TTL 안에 한 번 더 요청된 경우(두 번째 관측 = 실제 재사용)에만 캐시를 만든다. 살아 있는 항목을 늦게 재사용하면 TTL을 연장한다. `always`는 첫 사용부터 만들고(배치·eval), `never`는 만들지 않는다. `CallSpec.expected_reuse`가 계획된 재사용 횟수를 전달한다.

**패킷 배치.** 모든 패킷은 안정 블록(USER_LOCK·원자료·run 불변 상위 보고서)을 앞에, 가변 블록(RUN_HEADER·요청·리비전별 보고서·출력 계약)을 뒤에 둔다. 사용자 결정 메모는 출력 계약 직전에 붙는다. 같은 역할의 반복 호출(루프·복구·도구 2단계)이 동일 접두를 공유하므로 암묵 캐시를 지원하는 모델이 이를 재사용할 수 있고, Anthropic 프로바이더에서는 `cache_control` 접두 캐시에 그대로 대응한다.

종속항별 도면 비교는 루트 LOCK·설계·종속항 설계·스타일·OA 보고서와 원자료·도면을 공통 캐시에 저장한다. 각 호출에는 부모항 체인, 목표항, 독립 blind snapshot 및 해당 호출 식별자만 별도로 보낸다. 캐시 키는 역할·scope·모델·system instruction·공통 텍스트·도면 내용/MIME/라벨·run_id에 결속된다. 보고서의 revision과 원자료가 바뀌면 키도 바뀐다. 동일 프로세스의 병렬 목표항은 하나의 캐시 생성 결과를 공유한다.

캐시가 실패하거나 만료되면 기존 전체 입력으로 복구한다. blind 호출은 캐시 대상에서 제외한다. 로컬 calls의 packet_text에는 인계 전문을 그대로 남기며 context_transport에 공통 본문 길이·이미지 개수·이미지 전송 방식(files_api/inline)을 기록한다. 캐시는 반복 전송·처리 비용을 줄이는 방법이며, 모델이 받는 전체 문맥 길이나 추론 시간이 같은 비율로 줄어든다는 뜻은 아니다. 웹 라우터의 `CLAUDE.md` 계약도 캐시 가능한 소스 블록으로 전달된다.

숫자로 지정된 종속항 범위는 설계 후보, PRE_STYLE, 최종 스타일 세트에서 결정론적으로 대조한다. 범위 확대·누락·번호 중복은 REQUEST_SCOPE_MISMATCH로 중지하며 이전 PASS나 LOCK으로 우회하지 않는다. UI 옵션보다 현재 질문의 항 번호가 우선한다.

API 전달 방식 참고: [Gemini context caching](https://ai.google.dev/gemini-api/docs/caching).
