# ACE 개선 루프 (Agentic Context Engineering)

기존 RCA·eval·lessons 구조를 확장해 실패에서 절차 교훈을 만들고, 사람이 승인하고, 회귀 평가를 통과한
것만 역할 프롬프트에 주입하는 폐루프다. ACE 프레임워크 전체를 의존성으로 붙이지 않고 Reflector·Curator·
grow-and-refine(helpful/harmful) 개념만 이 저장소의 기존 구조 위에 얹었다.

```
정상 Claim-Agent 실행
      ↓  telemetry / records / state.feedback / eval 결과
Failure Miner   (improve/failures.py)   ← rca.py, feedback.py 재사용
      ↓
Reflector       (improve/reflect.py)
      ↓
Curator         (improve/curate.py)     → pending lesson + pending adversarial eval case
      ↓
Human Approval Inbox  (웹 ‘개선 / Approval Inbox’ · `claim-agent improve inbox`)
      ↓  승인 / 거절 / 수정 후 승인 / 보류
Regression Gate (improve/regression.py) ← experiments.Variant 재사용
      ↓  PASS
lessons/approved/ → 후속 run의 역할 프롬프트에 주입
```

## 청구항 파이프라인은 바뀌지 않는다

`pipeline/`(engine·locks·transitions·verify), `models/`, `roles/`, `.claude/agents/*.md`, `CLAUDE.md`,
`sources/`는 이 루프가 읽지도 고치지도 않는다. LOCK 생성 조건, revision·design_revision 의미론,
게이트 판정 규칙은 그대로다. ACE가 바꿀 수 있는 것은 **승인된 절차 교훈이 역할 프롬프트에 덧붙는 것**뿐이며,
그 경로는 이전부터 있던 `LessonStore.injection_text` 하나다.

## 운영 안전 규칙

1. Reflector·Curator는 역할 파일·`CLAUDE.md`·소스를 수정하지 않는다 (`tests/test_improve_ace.py`가 감시한다).
2. 자동 생성 교훈은 언제나 `pending`이다. 자동 승인은 없다.
3. 사람이 승인해도 회귀 평가를 통과하기 전에는 주입되지 않는다.
4. 발명의 기술내용은 교훈의 근거가 되지 않는다. 수치·부품명·`【청구항`·`상기` 같은 표현이 섞이면
   `reflect.tech_leak`이 걸러내고 교훈을 만들지 않는다.
5. 교훈은 절차·검수·표현상의 일반 규칙만 담는다.
6. `source_set_id`, lesson digest, revision 추적성은 그대로다 — 주입되는 집합(`lessons/approved/`)의
   정의를 바꾸지 않았기 때문이다.
7. 모든 자동 제안과 승인·거절은 `improve/audit.jsonl`에 남는다.
8. 파이프라인 엔진과 LOCK 생성 조건은 수정하지 않았다.

## 교훈의 상태

디렉터리가 주입 여부를 정한다는 기존 규칙은 유지된다. `lessons/approved/`에 있는 것만 주입된다.

| gate_status | 위치 | 주입 | 뜻 |
|---|---|---|---|
| `PENDING` | `lessons/pending/` | ✗ | Curator가 제안함 |
| `CANDIDATE_APPROVED` | `lessons/pending/` | ✗ | 사람이 승인함. 회귀 평가 대기 |
| `ACTIVE_APPROVED` | `lessons/approved/` | ✓ | 회귀 평가 통과 |
| `APPROVED_BUT_FAILED_EVAL` | `lessons/pending/` | ✗ | 승인했으나 회귀 실패 또는 판정 불가 |
| `HELD` | `lessons/pending/` | ✗ | 보류 |

`requires_eval: true`인 교훈(= ACE가 만든 것)은 `approve()`를 불러도 `CANDIDATE_APPROVED`에서 멈춘다.
`requires_eval`이 없는 기존 수기 교훈과 `lessons propose --text` 경로는 예전처럼 승인 즉시 주입된다.

## 회귀 평가가 리플레이로는 판정하지 못하는 이유

후보 교훈은 `lessons/approved/`에 넣지 않고 `Variant.prompt_suffix`로 프롬프트에만 얹어 baseline과
비교한다. 그런데 fixtures 리플레이는 프롬프트와 무관하게 녹화된 응답을 돌려주므로 교훈의 효과가
결과에 나타나지 않는다. 그래서 `mode: replay`에서는

- 기존 PASS 케이스 회귀·중지 증가·호출 수·토큰·루프 항목은 **엔진 회귀 검사**로서 유효하고,
- `목표 failure 탐지 개선` 항목은 `UNVERIFIED`로 남으며 전체 판정도 `UNVERIFIED`가 된다.

`UNVERIFIED`는 실패가 아니지만 활성화시키지도 않는다. 실제 활성화에는 승인된 적대 케이스와
`--mode live`가 필요하다.

## 지표

`claim-agent improve metrics` 또는 Inbox 상단. 최상위 품질 지표는 **Escaped-to-Lock Rate**다.

| 지표 | 정의 |
|---|---|
| Escaped-to-Lock Rate | LOCK까지 간 run 중 뒤에서 오류가 확인된 비율 |
| Failure Detection Rate | 파이프라인이 스스로 잡은 실패 / 전체 실패 |
| First Correct Detector Rate | 최초 탐지 지점이 기대 지점과 일치한 비율 |
| False Block Rate | 같은 revision에서 문언 변경 없이 뒤집힌 비-PASS 판정 비율 |
| Regression Rate | 직전 eval 결과에서 PASS였다가 최신에서 FAIL이 된 케이스 비율 |
| Lesson Hit Rate | 활성 교훈 중 활성화 이후 같은 failure mode가 재발하지 않은 비율 |
| Lesson Helpful / Harmful | 위 판정을 교훈별로 누적한 수 (`--update-scores`) |
| Mean Detection Stage | 실패가 실제로 잡힌 단계의 평균 좌표 (작을수록 이르다) |

표본이 작으면 추세로만 읽는다. 선행기술 없이 신규성·진보성을 단정하지 않는 것과 같은 원칙이다.

## 명령

```bash
claim-agent improve mine                   # 실패 수집 → 회고 → 큐레이션 (규칙 기반; --llm은 선택)
claim-agent improve inbox                  # 승인 대기 목록
claim-agent improve approve L-0001          # 승인 (아직 주입되지 않는다)
claim-agent improve edit-approve L-0001 --text "..." --roles claim-drafter
claim-agent improve approve --case adv-...  # 적대 케이스를 eval/cases/로 편입
claim-agent improve regress L-0001 --mode live
claim-agent improve metrics
claim-agent improve audit --limit 50
```

웹에서는 사이드바의 `◧ 개선 / Approval Inbox`가 같은 일을 한다.
