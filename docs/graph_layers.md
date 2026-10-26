# 3층 그래프 (`claim_agent/graph/`)

- 작성일: 2026-10-21
- 상태: 구현·엔진 연결됨. 설정은 모두 기본 `off`이다.

| 층 | 무엇 | 만드는 방법 | 받는 역할 | 근거 지위 |
|---|---|---|---|---|
| 1 규칙 그래프 | `sources/*.md`의 절·게이트·조건·역할 | 제목·대문자 게이트명·`조건 N`을 코드로 추출 (모델 호출 없음) | 설정 시 보조 파일을 쓰는 역할 | 기준 소스 그 자체 |
| 2 발명 그래프 | 구성·특징·과제·원리·효과·수치 | 모델 1회 호출로 추출한 뒤, 코드가 인용 원문을 글자 일치로 확인한 항목만 남김 | architect(아이디어 시트), 종속항 architect(+exam_pattern 힌트) | 원자료 위치만 근거. 묶음·사슬·대안은 `INFERRED — 근거 미확인` |
| 3 선례 그래프 | 등록 특허 30건의 보정 문구와 효과 주장 | cs2 `examples.json`의 보정 문구를 낱말 규칙으로 분류 | 종속항 architect **만** | 경험 통계. 게이트·근거·선행기술이 아님 |

## 1층: 규칙 묶음 (`graph.rule_pack: on`)

역할마다 **기준 파일은 전문**으로 보낸다(`rules.PRIMARY`). 예를 들어 OA는 06, style은 04·07이다. **보조 파일만** 그 역할이 이름으로 쓰는 게이트·조건을 정의하거나 언급하는 절로 줄인다. 절약이 10% 미만인 파일은 전문으로 보낸다. `claim-success-reviewer`는 소스 사용 계약을 감사하므로 줄이지 않는다.

처음에는 게이트 이름만으로 절을 고르게 해 봤다. 그러자 06의 CLAR·NOV·INV 항목처럼 게이트 이름 없이 판정 내용을 담은 절이 빠졌다. 그래서 기준 파일은 자르지 않는다.

측정 (`claim-agent graph rules`, 2026-10-21 소스 기준, 글자 수):

| 역할·범위 | 전문 | 묶음 | 절약 |
|---|---|---|---|
| syntax DEPENDENT_SET | 38,864 | 32,207 | 17.1% |
| pre-examiner PREEXAM_JUDGE | 38,513 | 33,027 | 14.2% |
| dependent architect | 54,355 | 49,770 | 8.4% |
| oa DEPENDENT_SET | 29,706 | 29,239 | 1.6% |
| 그 밖 | — | — | 0% |

토큰 절감은 작다. telemetry 205회 호출의 평균 프롬프트는 2만~3.7만 토큰이다. 그중 큰 몫은 원자료·상위 보고서·기준 파일인데, CLAUDE.md는 이들을 전문으로 전달하도록 정한다. 1층의 쓸모는 오히려 영향 분석(`--impact`)에 있다. 예를 들어 `claim-agent graph rules --impact "조건 4"`는 그 조건을 쓰는 절과 역할을 모두 보여 준다.

켜면 `source_set_id`에 `-rp1`이 붙어 기존 run이 STALE 처리된다. 판정이 같은지는 리플레이로 확인할 수 없다(sources_sha가 바뀌므로). `eval live --variant`로 실제 호출해 비교해야 한다.

## 2층: 발명 그래프 (`graph.invention: on`)

- ARCHITECT 전에 추출 호출을 1회 한다. 이 호출은 `graph.invention_role`(기본 `claim-architect`)의 프로바이더·모델을 빌린다. 결과는 `runs/<run>/graph/invention-<key>.json`에 저장하고 종속항 단계에서 다시 쓴다.
- 버린 항목은 `…-rejected.json`에 남는다. 버리는 경우는 원문에 없는 인용, 없는 끝점, 관계 종류에 맞지 않는 끝점이다.
- 시트는 패킷의 `### 출력 계약` 앞에 들어간다. 독립항 architect에는 exam_pattern 힌트를 넣지 않는다(설계 §13 #10).
- 추출 실패·파싱 실패는 run 노트만 남기고 시트 없이 진행한다.

합성 예시(케이블 클립 홀더)로 실제 1회 실행했다 (gemini-3.8-flash, 입력 951·출력 3,408 토큰).
- 노드 17개, 관계 18개, 버린 항목 0개.
- 사슬 3개가 모두 특징→원리→효과까지 닫혔다. 빠진 고리는 '과제' 하나다. 원자료가 문제를 따로 서술하지 않았으므로 발명자에게 확인할 질문 후보로 표시된다.

```bash
claim-agent graph invention --request-yaml eval/cases/sample-clip-holder/request.yaml --dependent --out out/clip
claim-agent graph ideas out/clip/graph.json --dependent
```

## 3층: 선례 그래프 (`graph.precedent: on`)

`graph.precedent_path`(기본 `../cs2-spec-writer/graph/prosecution_v2/examples.json`)를 읽기만 한다. 담고 있는 것은 사건 30건, 보정 문구 99개, 효과 주장 6개, 기재불비 지적 27건이다.

발명 그래프의 사슬마다 exam_pattern 힌트가 같은 보정 문구를 글자 유사도순으로 2개씩 보여 준다. 패턴별 통계도 함께 낸다. 통계 항목은 진보성 거절 뒤 청구항 1에 더해진 수, 최초 종속항에서 온 수다.

```bash
claim-agent graph precedent                       # 패턴 통계
claim-agent graph precedent --graph out/clip/graph.json
```

## 엔진 연결

`pipeline/engine.py`의 4곳에서 패킷에 시트를 덧붙인다.

1. `__init__`: `self.graph_hooks = GraphHooks(cfg, provider, store.run_dir, self._account_result)`
2. `_independent_step` ARCHITECT: `pk.text = self.graph_hooks.architect_packet(state, bundle, pk.text)` — 아이디어 시트만
3. `_dependent_step` DEP_ARCHITECT의 `dep_architect(...)` 두 곳: `pk.text = self.graph_hooks.dependent_packet(state, bundle, pk.text)` — 아이디어 시트(exam_pattern 힌트 포함)와 심사 이력 참고 시트

나머지 역할(drafter·style·success·syntax·OA·blind·picture)의 패킷에는 어느 시트도 들어가지 않는다. `tests/test_graph.py::test_pipeline_puts_sheets_in_architect_packets`가 이를 확인한다.

켜는 법 (`claim-agent.yaml`):

```yaml
graph:
  invention: on        # 아이디어 시트 (추출 호출 1회)
  precedent: on        # 심사 이력 참고 시트 (종속항 설계자만)
  invention_role: claim-architect   # 추출 호출이 빌릴 역할의 프로바이더·모델
```
