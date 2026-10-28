# DRAFT_DEPENDENT_SET_LOCK — DS-hl-ctrl-1519 / dd2 / dr4 (2026-10-28)

정식 명세서 뒷받침·실시가능성 미검증 잠정안. 출원용 최종 종속항 세트 아님.

- request_mode: AUTHORING_DRAFT / EXISTING_SET_EDIT / 편집 대상 제15~19항(번호·인용 유지)
- 루트 기준: BASELINE_SET — UNVERIFIED (사용자 원문 1~19항, sha256 3406c8cc46149394c81bd9d4d7f0ba273d759c63993d36cae183b6de17063873, 루트 제14항)
- dependent_set_id DS-hl-ctrl-1519 / dependent_design_revision dd2 (DEPENDENT_DESIGN_GATE: LOCKED) / dependent_revision dr4
- dependent_meaning_draft_id: dmd-DS-hl-ctrl-1519-dr4-01
- dependent_style_record_id: dsr-DS-hl-ctrl-1519-dr4-01 — CLAIM_STYLE_GATE PASS, TERM_EXPRESSION_GATE PASS, 변경 대조표(표지 제거·줄바꿈만, 코드 대조 IDENTICAL), 출처표(dr4_style_record.md)
- dependent_success_record_id: dsr-DS-hl-ctrl-1519-dr4-02 — PASS
- syntax: PASS (시험 1~22)
- DEPENDENT_OA_DRAFT_GATE: PASS (D9 철회 후) / DEPENDENT_OA_FINAL_GATE: UNVERIFIED — SPEC_NOT_PROVIDED → FINAL LOCK 불가
- 기술기여: DC-15·16 TECHNICAL_SOLUTION_CANDIDATE(세 게이트+EV1~5 PASS), DC-17·18·19 FALLBACK_ONLY(SOURCE·CLAIMABILITY PASS)
- NON_PATENT_TECHNICAL_READER_GATE: PASS / GEOMETRIC_OBJECT_GATE: 16항 PASS, 나머지 N/A / FUNCTION_DEDUCTION_GATE: 15·16 PASS, 17~19 N/A
- blind: dbs-DS-hl-ctrl-1519-dr4-c15-01, -c16-01, -c17-01, -c18-01, -c19-01
- picture: 15~19 모두 PASS-RANGE → DEPENDENT_RECONSTRUCTION_GATE: PASS
- PRIOR_ART_SET: NONE → INVENTIVE_STEP UNVERIFIED — PRIOR_ART_NOT_PROVIDED
- 경과: dd1 → dr1(syntax BLOCK·OA REVIEW → DEPENDENT_ARCHITECT 1/1) → dd2 → dr2(syntax BLOCK → DRAFTER 1/2) → dr3(success·syntax BLOCK·OA REVIEW → DRAFTER 2/2) → dr4 LOCK.

## 잠긴 문언 (exact) — dr4_claims.md와 동일

(dr4_style_record.md "최종 문언 dr4 (exact)" 참조)

## 비게이팅 잔존
- Q1 접속부 단자 절연·전원 단자 용도·자리별 배선 / Q4 신호 내용·발생원 / Q5 서브 단자 상대편·PCB 패드 4개 — 17~19항은 사용자 초안 기재만, 도면 미확인.
- K5 문형 반복(16항 4회·17항 3회), 17항 "단자 중 … 중 어느 것도 아닌 단자" 중첩 — 읽기 부담(dd2 §2 고정).
- 17항 "상기 복수의 포고 핀은, 결합되면 각각 …" 서술 귀속: picture 선택 수정 후보(전원 핀 서술을 별도 절로) — 필수 아님.
- 19항 "상기 제어부는 … 상기 제어부에 이르는" 자기 지칭(dd2 DC-19).
- 5항 "서브 단자"(포고 핀과 분리)와 18항 "서브 단자"(신호 포고 핀과 연결) 의미 긴장 — 편집 범위 밖.
- 계약 미기재 구현 자유도: "서로 전기적으로 분리된" 범위(공통 접지), 16항 플레이트 위치·면 방향, 19항 연결 시점·중간 소자.
