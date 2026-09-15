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

fixtures는 `--record` 시점의 프롬프트·소스 해시를 담고 있으며, 프롬프트가 바뀌어도 느슨한 리플레이(역할·scope·phase 순서)로 엔진 회귀를 검사한다. 프롬프트 품질 자체는 `eval live`로 주기적으로 다시 측정한다.
