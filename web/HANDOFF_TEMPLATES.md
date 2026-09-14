<!-- claim-agent-bundle: 2026.08.27.5 -->

# Claim-Agent Web — 버전·인계 템플릿

## RUN_HEADER

```text
bundle_version: 2026.08.27.5
protocol_version: 1.4.0
source_set_id: cc-web-2026.08.27.5
source_manifest_digest: <BUNDLE_MANIFEST.md의 값>
execution_profile: WEB_SINGLE_CHAT | WEB_ISOLATED_CHATS
run_id: run-YYYYMMDD-NN
request_mode: AUTHORING_DRAFT | FINALIZATION | REVIEW_ONLY | META
input_revision: iN
candidate_id: <stable-id>
revision: rN
design_revision: dN | N/A
dependent_set_id: <stable-id> | N/A
dependent_design_revision: ddN | N/A
dependent_revision: drN | N/A
PRIOR_ART_SET: <사용자 식별명 목록> | NONE
```

## 공통 게이트 기록

```text
record_type: DESIGN | MEANING_DRAFT | STYLE | TERM | SUCCESS | SYNTAX | OA | RECONSTRUCTION | REFERENCE_COMPARE | DEPENDENT_DESIGN | DEPENDENT_MEANING_DRAFT | DEPENDENT_STYLE | DEPENDENT_SUCCESS | DEPENDENT_SYNTAX | DEPENDENT_OA | DEPENDENT_RECONSTRUCTION | DEPENDENT_REFERENCE_COMPARE
record_id: <type>-<candidate_id>-<revision>-NN | <type>-<dependent_set_id>-<dependent_revision>-NN
bundle_version: 2026.08.27.5
source_set_id: cc-web-2026.08.27.5
source_manifest_digest: <BUNDLE_MANIFEST.md의 값>
run_id: <run-id>
input_revision: iN
candidate_id: <stable-id>
revision: rN
design_revision: dN
dependent_set_id: <stable-id> | N/A
dependent_design_revision: ddN | N/A
dependent_revision: drN | N/A
meaning_draft_id: <ID> | N/A
dependent_meaning_draft_id: <ID> | N/A
style_record_id: <ID> | N/A
dependent_style_record_id: <ID> | N/A
review_context: SAME_AGENT_SEQUENTIAL | FRESH_ISOLATED_CHAT
success_scope: INDEPENDENT | DEPENDENT_SET | N/A
exact_claim_text: |
  <독립항 전문 또는 부모항 체인을 포함한 종속항 세트 전문>
inputs_used:
  - <식별명 또는 기록 ID>
decision: PASS | PASS-RANGE | REVIEW | BLOCK | UNVERIFIED
```

## DRAFT_CLAIM_LOCK

```text
record_type: DRAFT_CLAIM_LOCK
lock_id: dcl-<candidate_id>-<revision>-NN
lock_class: DRAFT-SELF | DRAFT-ISOLATED
bundle_version: 2026.08.27.5
protocol_version: 1.4.0
source_set_id: cc-web-2026.08.27.5
source_manifest_digest: <BUNDLE_MANIFEST.md의 값>
execution_profile: WEB_SINGLE_CHAT | WEB_ISOLATED_CHATS
run_id: <run-id>
input_revision: iN
candidate_id: <stable-id>
revision: rN
design_revision: dN
USER_LOCK: <전문 또는 없음>
root_claim_text: |
  <전문>
invention_sources:
  - <사용자 식별명>
design_record_id: <ID>
meaning_draft_id: <ID>
style_record_id: <ID>
claim_style_gate: PASS
style_change_table:
  - <입력 구절 / 출력 구절 / 적용 규칙 / 범위 영향 / 판정>
success_record_id: <ID>
term_expression_gate: PASS
non_patent_technical_reader_gate: PASS
geometric_object_gate: PASS | NOT_APPLICABLE
syntax_record_id: <ID>
syntax_decision: PASS
syntax_review_context: SAME_AGENT_SEQUENTIAL | FRESH_ISOLATED_CHAT
oa_record_id: <ID>
oa_draft_gate: PASS
oa_final_gate: <판정>
blind_snapshot_id: <ID 또는 N/A>
self_snapshot_id: <ID 또는 N/A>
independence: NONE — SAME_CONTEXT_SELF_REVIEW | FRESH_CHAT_NO_PROJECT_CONTEXT
reference_compare_record_id: <ID>
reference_compare_decision: PASS | PASS-RANGE
assurance_note: <잠금 등급 한계>
unverified:
  - <항목>
status_label: 명세서 뒷받침·실시가능성 미검증 잠정안
```

## 종속항 세트 LOCK

```text
record_type: DRAFT_DEPENDENT_SET_LOCK | FINAL_DEPENDENT_SET_LOCK
lock_id: ddsl-<dependent_set_id>-<dependent_revision>-NN | fdsl-<dependent_set_id>-<dependent_revision>-NN
lock_class: DRAFT-SELF | DRAFT-ISOLATED | FINAL-ISOLATED
bundle_version: 2026.08.27.5
protocol_version: 1.4.0
source_set_id: cc-web-2026.08.27.5
source_manifest_digest: <BUNDLE_MANIFEST.md의 값>
execution_profile: WEB_SINGLE_CHAT | WEB_ISOLATED_CHATS
run_id: <run-id>
input_revision: iN
root_lock_id: <DRAFT_CLAIM_LOCK 또는 FINAL_CLAIM_LOCK ID>
candidate_id: <root stable-id>
revision: rN
design_revision: dN
dependent_set_id: <stable-id>
dependent_design_revision: ddN
dependent_revision: drN
USER_LOCK: <전문 또는 없음>
root_claim_text: |
  <독립항 전문>
dependent_claim_set_text: |
  <종속항 세트 전문>
invention_sources:
  - <사용자 식별명>
PRIOR_ART_SET: <사용자 식별명 목록> | NONE
dependent_design_record_id: <ID>
dependent_design_gate: LOCKED
dependent_meaning_draft_id: <ID>
dependent_style_record_id: <ID>
claim_style_gate: PASS
dependent_style_change_table:
  - <목표항 / 입력 구절 / 출력 구절 / 적용 규칙 / 범위 영향 / 판정>
technical_solution_candidates:
  - <DC-NN>
spatial_object_contracts:
  - <DC-NN / 실제 객체 / 관찰 기준 / 단면 대상 / 형상 주체 / 방향·개방 대상>
excluded_drawing_only:
  - <DC-NN 또는 없음>
dependent_success_record_id: <ID>
term_expression_gate: PASS
non_patent_technical_reader_gate: PASS
geometric_object_gate: PASS | NOT_APPLICABLE
dependent_syntax_record_id: <ID>
dependent_syntax_decision: PASS
dependent_syntax_review_context: SAME_AGENT_SEQUENTIAL | FRESH_ISOLATED_CHAT
dependent_oa_record_id: <ID>
dependent_oa_draft_gate: PASS
dependent_oa_final_gate: <판정>
dependent_snapshots:
  - target_claim_id: <ID>
    snapshot_id: <dependent_blind_snapshot_id 또는 dependent_self_snapshot_id>
    independence: NONE — SAME_CONTEXT_SELF_REVIEW | FRESH_CHAT_NO_PROJECT_CONTEXT
    reference_compare_record_id: <ID>
    reference_compare_decision: PASS | PASS-RANGE
dependent_reconstruction_gate: PASS
inventive_step: PASS | REVIEW | BLOCK | UNVERIFIED
prior_art_scope: <검토 범위 또는 NONE>
assurance_note: <잠금 등급 한계>
unverified:
  - <항목>
status_label: 명세서 뒷받침·실시가능성 미검증 잠정안 | 출원용 최종 종속항 세트
```

## 격리 success/syntax/OA 대화 인계 머리말

```text
handoff_type: STYLE_ADJUSTMENT | SUCCESS_REVIEW | SYNTAX_REVIEW | OA_REVIEW | DEPENDENT_STYLE_ADJUSTMENT | DEPENDENT_SUCCESS_REVIEW | DEPENDENT_SYNTAX_REVIEW | DEPENDENT_OA_REVIEW | DEPENDENT_BLIND_RECONSTRUCTION | DEPENDENT_REFERENCE_COMPARE
bundle_version: 2026.08.27.5
protocol_version: 1.4.0
source_set_id: cc-web-2026.08.27.5
source_manifest_digest: <BUNDLE_MANIFEST.md의 값>
run_id: <run-id>
input_revision: iN
candidate_id: <stable-id>
revision: rN
design_revision: dN
dependent_set_id: <stable-id> | N/A
dependent_design_revision: ddN | N/A
dependent_revision: drN | N/A
```

인계 본문에는 대상 역할 파일이 요구하는 입력 전문을 빠짐없이 붙인다. 요약만 전달해 PASS를 만들지 않는다. 식별자 또는 exact 청구항 전문이 다르면 `INPUT_REVISION_MISMATCH`다.
