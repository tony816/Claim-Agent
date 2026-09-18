# 골든 평가 세트

`eval/cases/<case_id>/`는 실제 발명 1건과 그 기대 결과를 담는 골든 케이스다. 파이프라인·프롬프트·모델을 바꿀 때 "좋아졌는지"를 같은 잣대로 비교하는 유일한 기준이므로, 발명 유형(PHYSICAL·PROCESS·DATA_CONTROL·COMPOSITION·HYBRID)별로 최소 1건씩 두는 것을 목표로 한다.

```
eval/cases/<case_id>/
  request.yaml      # 요청·원자료 경로·종속항 범위 (eval/cases/README 참조)
  expected.yaml     # 기대 게이트·주골격 용어·금지 용어·역구성 판정·호출 상한
  sources/          # 발명 설명·도면·선행기술 (기밀이면 커밋하지 말고 .gitignore)
  fixtures/         # `--record`로 녹화한 실제 응답 (sanitize 후 커밋) → CI replay
```

## 만들기

```bash
claim-agent eval new my-invention                 # 디렉터리·템플릿 생성
# sources/에 자료 넣고 request.yaml·expected.yaml 채우기
claim-agent eval live --case my-invention --record eval/cases/my-invention/fixtures   # 예상 비용 확인 후 1회 실제 호출
claim-agent fixtures sanitize eval/cases/my-invention/fixtures                         # 요청 미리보기 제거
claim-agent eval run --case my-invention          # 이후는 fixtures 리플레이 (API 호출 없음, CI가 실행)
```

## expected.yaml 필드

| 필드 | 검사 |
|---|---|
| `outcome` | run의 최종 outcome과 일치 |
| `gates.STAGE.GATE: [허용값]` | 해당 단계 마지막 기록의 게이트 값 |
| `expected_invention_type` | 설계자의 `invention_type.primary` |
| `reconstruction: [PASS, PASS-RANGE]` | 독립항 picture 판정; 종속항 포함이면 `DEPENDENT_RECONSTRUCTION_GATE: PASS`도 검사 |
| `skeleton_terms` | 주골격의 SOURCE_EXACT_TERM이 exact 문언에 모두 존재 |
| `forbidden_terms` / `must_not_contain` | 즉석 조어·CONCEPT_LABEL_ONLY 부재 |
| `must_contain` | 문자열 포함 |
| `user_lock_preserved: true` | USER_LOCK 문언이 최종 문언에 그대로 있음 |
| `max_loops`, `max_calls`, `max_output_tokens_total` | 상한 |
| `design_revision` | 독립항 design_revision 값 (기존 세트 편집은 `"N/A"`) |
| `independent_stages_run: false` | 독립항 단계(ARCHITECT~LOCK) 기록이 하나도 없음 |
| `dependent_claim_nos: [9]` | 산출 종속항 번호 집합이 정확히 일치 |
| `baseline_unchanged: true` | 기존 세트 편집에서 편집 대상 외의 항이 원문 그대로 반환됨 |
| `halt_contains` | 중지 사유 코드·메시지에 포함될 문자열 |

`authoring_scope: EXISTING_SET_EDIT` 케이스(`existing-set-edit-9`, `dep-target-single-9`, `no-merge-guard`)는 `claim_file`의 번호 세트에서 `dependent_target` 항만 고친다. fixtures가 없어 CI 리플레이에서는 SKIP되며, `tests/test_existing_set_edit.py`가 scripted 역할로 같은 기대값을 검사한다.

fixtures는 `--record` 시점의 프롬프트·소스 해시를 담고 있으며, 프롬프트가 바뀌어도 느슨한 리플레이(역할·scope·phase 순서)로 엔진 회귀를 검사한다. 프롬프트 품질 자체는 `eval live`로 주기적으로 다시 측정한다.

## 적대 평가 케이스 (ACE)

`eval/candidates/<case_id>/`는 **승인 전** 적대 케이스다. `eval/cases/*/request.yaml` 글로브에 걸리지 않으므로
CI와 `eval run --all`은 승인 전 케이스를 실행하지 않는다. 웹의 `개선 / Approval Inbox` 또는
`claim-agent improve approve --case <id>`로 승인하면 `eval/cases/`로 복사되어 정식 회귀 세트가 된다.

적대 케이스는 **통과가 정답이 아니라 걸리는 것이 정답**이다. 그래서 seed의 `outcome`·게이트 PASS 기대를 버리고
아래 필드를 쓴다.

| 필드 | 검사 |
|---|---|
| `adversarial: true` | 이 블록의 검사를 켠다. 없으면 일반 골든 케이스로 동작한다 |
| `mutation_type` | 주입한 변형의 종류 (`curate.MUTATION_TYPES`) |
| `seed_case` | 변형의 바탕이 된 정상 케이스 |
| `injected_defect` | 사람이 읽는 결함 설명 |
| `expected_first_detector: {role, stage, gate}` | 최초로 비-PASS를 내야 하는 지점 |
| `must_not_pass_gates: [GATE]` | 이 게이트가 PASS로 끝나면 실패 |
| `expected_return_to` | 기대 되돌림 경로 (`RETURN_TO_*`) |
| `escaped_to_lock_must_be: false` | LOCK까지 가면 실패 |

결함은 기계적으로 주입할 수 없으므로 seed의 `request_text`에 변형 지시를 덧붙이는 방식으로 만든다.
따라서 **fixtures 리플레이로는 의미가 없다**(녹화된 응답이 지시와 무관하게 재생된다). 적대 케이스는
`eval live`로 측정한다.
