<!-- adapter_version: 2 -->
# 런타임 어댑터 (Claim-Agent Python + Gemini 실행 프로필)

이 호출은 Claude Code 서브에이전트가 아니라 Python 오케스트레이터가 Gemini API로 실행하는 단일 역할 호출이다. 아래 규칙은 뒤따르는 역할 파일의 실행 계약을 대체하지 않고, 도구·소스 접근 방식과 출력 형식만 이 환경에 맞게 바꾼다.

1. `Read`, `Glob`, `Grep` 도구는 없다. 역할 파일이 읽으라고 지정한 소스 파일은 오케스트레이터가 `<<<FILE 경로 sha256=…>>>` … `<<<END FILE>>>` 블록으로 이 호출에 사전 로딩했다. 이 블록을 실제로 읽은 것으로 취급하고, `소스 로딩 기록`에는 그 경로만 기재한다. 사전 로딩되지 않은 파일은 존재하지 않는 것으로 취급하며 내용을 추정하지 않는다.
2. 현재 발명의 원자료·USER_LOCK·선행 단계 보고서 전문은 `## 입력 패킷` 아래에 제공된다. 패킷에 없는 자료를 요구하거나 상상해서 채우지 않는다. 필수 입력이 없으면 역할 파일의 규칙대로 `BLOCK`, `REVIEW` 또는 `UNVERIFIED`와 정확한 사유 코드를 반환한다.
3. `candidate_id`, `revision`, `design_revision`, `dependent_*`, `record_id` 등 식별자는 오케스트레이터가 지정한다. 새로 만들지 말고 그대로 echo한다. `record_id`는 오케스트레이터가 예정한 값이며, 판정이 PASS일 때만 발급된 것으로 기록된다.
4. 사용자에게 질문하지 않는다. 사용자 판단이 필요하면 `status: REVIEW` 또는 `next_step: USER_DECISION`과 정확한 쟁점을 `open_issues`에 적는다.
5. 출력은 제공된 JSON 스키마를 따르는 JSON 객체 하나뿐이다. `report_markdown` 필드에는 역할 파일의 `출력 형식` 항목을 빠짐없이, 한국어로, 표와 전문을 포함해 그대로 작성한다. `status`, `gates`, `next_step`, `handoff_ready`, `exact_claim_text`는 `report_markdown`의 내용과 글자 단위로 일치해야 한다. 특히 `exact_claim_text`에는 청구항 전문을 줄바꿈·문장부호까지 그대로 넣는다.
6. `gates`에는 이 역할이 판정하는 게이트만 채우고, 판정하지 않는 게이트는 null로 둔다. 사유가 붙는 판정(예: `OA_FINAL_GATE: UNVERIFIED — SPEC_NOT_PROVIDED`)은 `gates`에 상태만, `gate_reasons`에 사유 코드를 적는다. `checks`에는 조건별·시험별 세부 판정을 적는다.
7. 역할 파일이 `한정별 근거` 또는 근거표(조건 4)를 요구하면, 보고서의 표와 같은 내용을 `limitation_evidence[]`에도 행 단위로 넣는다: `limitation`(한정 문언), `role`(F/E/C/N/I/S), `source_name`(원자료 파일명 또는 USER_LOCK), `location`(단락·도면·표 위치), `basis`(`DIRECT` 직접 기재 / `DERIVED` 통상의 기술자가 명확히 도출 / `UNCONFIRMED` 근거 미확인), `note`. `UNCONFIRMED` 행이 하나라도 있으면 상태를 `PASS`로 하지 않는다. 오케스트레이터는 PASS와 UNCONFIRMED 행이 함께 오면 그 판정을 발급하지 않고 REVIEW로 중지한다.
8. `sources_read`에는 사전 로딩 블록의 경로만 적는다. 승인된 프로젝트 교훈(있는 경우)은 절차·표현 주의사항일 뿐 발명의 기술내용 근거가 아니며, 역할 파일과 CLAUDE.md 규칙보다 우선하지 않는다.
