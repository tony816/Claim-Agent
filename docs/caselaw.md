# 판례 DB와 청구항 작성

- 작성일: 2026-10-22
- 코드: `claim_agent/graph/caselaw.py`, CLI `claim-agent graph caselaw …`

판례는 아래 곳에 쓴다. 어느 쪽도 현재 발명의 원자료가 아니고 게이트 값을 바꾸지 않는다. 판례만으로 신규성·진보성을 판정하지 않는다(선행기술 대비가 필요하다).

| 쓰는 곳 | 무엇 | 받는 역할 |
|---|---|---|
| 작성 전 주의 시트 | `sources/11_판례_청구항_주의시트.md` (S11) — 법원이 불명확·불일치·좁게 해석한 청구항 문언 유형 | claim-architect, claim-drafter (success reviewer는 사용 경계 감사만) |
| 권리행사 작성 기준 | `sources/13_판례_권리행사_작성기준.md` (S13) — 침해·권리범위 판단을 가른 청구항 문언 유형 W01~W14 | claim-architect, claim-drafter, 종속항 설계자 (success reviewer는 감사만) |
| 권리행사 점검표 | `caselaw enforce` — exact 문언에서 W02·W04·W05·W09·W12·W01 유형을 코드로 찾아 판례를 붙임 | OA (엔진은 `graph.caselaw_review: on`이면 자동) |
| 특허성 유형 기준 | `sources/12_판례_특허성_유형기준.md` (S12) — 특징 유형 F-…별 진보성·신규성 인정·부정 이유와 원자료 확인 질문 | 사전 심사 JUDGE, 종속항 설계자, OA. 독립항 architect·drafter·style에는 주지 않음(조건 8) |
| 특허성 판례 신호표 | `caselaw features` — 특징마다 유형, 법원 판단 분포, 인정 요인별 원자료 확인 질문, 비슷한 판례 | 사용자 보고, JUDGE, 종속항 설계자 |
| 검수 지적의 참고 근거 | syntax·OA 지적마다 같은 종류의 결함을 판단한 판례 | 오케스트레이터가 지적에 붙여 사용자에게 보고 |
| 작업 도구의 판례 도구 | 웹 대화 오른쪽 패널의 권리행사 점검·판례 찾기·특허성 신호 (`claim_agent/graph/caselaw_tool.py`) | 사용자만. 대화 기록·원자료·검수 대상에 넣지 않아 어느 모델도 읽지 않음 |

## 데이터 배치

cs2-spec-writer의 검증된 배포본을 그대로 복제한다. `graph/`는 git에서 제외되므로 다른 PC에는 따로 복사한다.

```text
graph/patent_case_law/
  graph.sqlite            (521MB, SHA-256 48ac0002…4d5b)
  RELEASE_MANIFEST.json   (case-graph-20260930-v2, pack pdfkg-f14e7eee8ccf39a8)
  pdfs/DOC-….pdf          (1,163개)
docs/caselaw-lessons.json (교훈 기록 352개, cs2 9fb76e9에서 복사 — git에 포함)
```

경로는 `claim-agent.yaml`의 `graph.caselaw_dir`, `graph.caselaw_lessons`로 바꿀 수 있다. DB는 `mode=ro`·`query_only`로만 열고, SHA-256·release·manifest·행 수가 맞지 않으면 열지 않는다.

```bash
claim-agent graph caselaw check          # 빠른 검사 (--full: SQLite integrity_check 추가)
```

## 주의 시트 (S11)

교훈 기록에서 코드로 만든다. 손으로 고치지 않는다. 기록이 바뀌면 다시 만든다.

```bash
claim-agent graph caselaw sheet --write
```

- 넣는 것: 명확성 위반 28건(태그 C1~C6·C8), 명확하다고 본 표현 28건(한 줄), 뒷받침 위반 15건, 권리범위 P2 청구범위 해석·P4 기능적 표현·P5 용어 정의(불리 사례 전문, 유리 사례 한 줄). 약 1.5만 자다.
- 넣지 않는 것: 판례 인용문, 교훈 문장(명세서 작성용 지시라 청구항 작성에 그대로 쓰면 범위가 바뀐다), 실시가능·신규사항·과제해결원리(P1) 사례.
- C 태그 이름은 기록 내용을 보고 붙인 라벨이다. 원본에는 'C1~C8: 명확성 분류'만 있다.
- S11이 생기면서 `source_set_id`가 바뀌어 이전 run은 STALE 처리된다.

## 지적별 판례 조회

```bash
claim-agent graph caselaw find "청구항 1의 '대략 수평으로'는 기준면과 허용 각도를 원자료가 정하지 않음" --group 명확성 --json
claim-agent graph caselaw show 2021허193
```

1. 후보 고르기: 지적 문장과 기록의 청구항 문언·결정 사실의 글자 2-gram 겹침으로 상위 k개(기본 5)를 고른다. 형태소 분석기는 쓰지 않는다.
2. 관련성 확인: 후보마다 `jev_question`(같은 조문이 아니라 '무엇이 빠졌는지가 같은가')을 Jev noul로 묻고 0.5 이상만 남긴다. 0.7 이상은 'Jev 확인', 그 사이는 '약한 관련'이다.
   - `--jev`는 jev-cli와 `.env`의 `OPENROUTER_API_KEY`가 있을 때 한 요청으로 묻는다.
   - 키가 없는 PC(지금 이 PC)에서는 오케스트레이터가 `--json` 출력의 `jev_question`과 후보 하나씩을 Jev 커넥터 `noul`에 넣는다. 2026-10-22 실측: '대략 수평' 지적에 '내외' 위반 판례 0.89, 일반 명확성 충족 판례 0.20.
3. 인용 확인: 출력의 인용문은 DB `pdf_pages`의 그 쪽 `[quote_start:quote_end]`와 글자 단위로 다시 대조한다(`quote_matches_db`). 352/352 일치.

결과는 `판례 참고 (미검토 — 게이트·근거 아님)`으로 지적에 붙인다. 판례가 붙었다고 판정을 올리거나 낮추지 않고, drafter·style adjuster에 판례 원문을 전달하지 않는다.

## 특허성·권리행사 기록 추출

```bash
claim-agent graph caselaw extract --workers 6          # 캐시 없는 사건만 호출 (기본 claude_oauth = Claude 구독 CLI, 프로젝트 기본 프로바이더와 별개)
claim-agent graph caselaw extract --records-only       # 호출 없이 캐시로 기록 파일만 다시 만들기
claim-agent graph caselaw sheet --kind patentability --write
claim-agent graph caselaw sheet --kind enforcement --write
```

- 대상: DB 사건 색인에서 특허성 쟁점(진보성·신규성·결합동기·수치한정·선택·용도·파라미터 등)이나 권리행사 쟁점(권리범위확인·균등·문언·간접침해·청구범위 해석·기능적 청구항·전제부·자유실시기술 등)이 붙은 문서 830건.
- 한 사건에 모델 호출 1회. 관련어가 나오는 쪽과 그다음 쪽만 보내고 36,000자에서 자른다. 답은 `graph/patent_case_law/extract/raw/<문서>.json`에 캐시한다.
- 인용 확인: PDF 텍스트층은 숫자·문장부호·영문 약어를 엉뚱한 자리로 옮겨 두고, 모델은 인용할 때 이를 바로잡는다. 그래서 한글 글자열만 비교해 원문에서 다시 찾고(쪽 경계를 넘으면 다음 쪽과 이어서), 저장하는 인용문은 DB 원문 `[quote_start:quote_end]`다. 찾지 못한 기록은 버리고 `graph/patent_case_law/extract/rejected.json`에 남긴다.
- 특허성 기록: `requirement`(신규성·진보성), `outcome`(부정·인정), `type_code`(F-SHAPE 등 14개, 06 INV 항목과 연결), `reason_factors`(법원이 든 이유 15종), `claim_feature`, `other_side`(선행발명), `deciding_reason`, `evidence_basis`(법원이 근거로 삼은 명세서 기재·부재).
- 권리행사 기록: `requirement`, `outcome`(특허권자 유리·불리), `type_code`(W01~W14), `claim_feature`(문언), `other_side`(확인대상발명), `deciding_reason`, `drafting_lesson`(판결 이유에서만 끌어낸 청구항 작성 교훈).
- `quote_is_argument`: 인용문에 '주장한다' 같은 말이 있으면 표시한다. 시트는 이런 기록을 뒤로 미룬다.

## 특징별 특허성 신호와 권리행사 점검

```bash
claim-agent graph caselaw features --feature "가열 온도 300~400℃ 범위::F-NUMERIC" --feature "센서 신호로 모터를 자동 정지" --json
claim-agent graph caselaw features --features-file design_elements.json --json   # [{id, text, type_code?}]
claim-agent graph caselaw enforce --claim-file claims.txt                          # 【청구항 N】 표시가 있는 문언
claim-agent graph caselaw find "기능적 표현이 실시예로 좁게 해석" --set enforcement --type W02 --json
```

- 유형을 주지 않으면 낱말 규칙(수치·재질·제어 등)으로, 맞는 낱말이 없으면 비슷한 기록의 다수 유형으로 추정하고 그렇게 표시한다. 오케스트레이터나 JUDGE가 `TEXT::F-…`로 유형을 주는 쪽이 정확하다.
- 판단 분포는 법원에 온 사건의 분포다. 등록 가능성 통계가 아니다.
- `enforce`는 코드 규칙으로만 찾는다. 기능적 표현(W02), 구성을 나열한 전제부(W04), 수치 범위(W05), 여러 실시 주체·사용자 행위(W09), 제조방법으로 특정한 물건(W12), 배타·부정 한정(W01). 질문일 뿐 판정이 아니다.
- `--jev`나 `--json`의 `jev_question`으로 판례 관련성을 확인하는 방식은 지적별 조회와 같다.

## 작업 도구의 판례 도구 (웹)

웹 대화의 작업 도구 패널에서 고르고 입력창의 Enter로 실행한다. 두 작업 방식 모두에서 쓰며 요청 분류·청구항 역할을 거치지 않는다. 사용법은 `사용법.md`의 작업 도구 패널 항목에 있다.

| 도구 | 입력 | 하는 일 |
|---|---|---|
| 권리행사 점검 | 대상 청구항 블록 | `enforcement_report`와 같은 코드 규칙(W01·W02·W04·W05·W09·W12)으로 문언을 찾고 같은 유형의 판례 3개를 후보로 둔다 |
| 판례 찾기 | 입력창의 지적·문언 | 교훈·권리행사·특허성 기록마다 글자 겹침 3개에, 문장으로 짐작한 유형마다 2개를 더한다. 교훈은 `TAG_WORDS`(예: '대략'→C3, '기준면·각도'→C4), 권리행사는 입력 문장의 `enforcement_flags` 유형이다 |
| 특허성 신호 | 입력창의 특징(한 줄에 하나, 8개까지, `::F-…` 유형 선택) | `feature_signals`와 같다 |

- **관련성:** 모든 후보를 Jev 한 요청(`confirm_pairs`)으로 묻고 0.5 미만은 뺀다. 지적이 하나면 `confirm`과 같은 state다. 쌍마다 지적을 넣은 state는 같은 판례에 훨씬 무딘 확률을 냈다(2026-10-29 실측 '대략 수평' 지적: '내외' 판례 0.56 / 무관 0.39 대 confirm 형식 0.80 / 0.18). 실측 비용 $0.0003, 1.5~2초.
- **Jev를 못 쓰면:** 후보를 `Jev 미확인`으로 그대로 보이고 이유를 적는다.
- **판례 DB가 없으면:** 기록 파일만으로 답하고 인용문 원문 대조를 생략했다고 적는다.
- **모델에 가지 않음:** 답과 그 요청은 `mode: CASELAW` 메시지로 대화에 남지만 `history.json`에 쓰지 않는다. 다음 요청의 원자료·이전 요청·참조와 검수 대상은 `web.conversational()` 한 곳에서 이 메시지를 뺀다. 판례 원문을 작성 역할·일반 에이전트에 넘기지 않는 규칙(CLAUDE.md)을 웹에서 지키는 자리다.

## 한계

- 쟁점·결정 사실은 모델 요약이고 사람 법리 검토 전이다.
- 조회 대상은 교훈 기록 352개(판례 274건)다. DB 전체 7,739청크 전문 검색은 아직 없다.
- 엔진(`claim-agent run`)은 S11·S12·S13과 OA 권리행사 점검표를 패킷에 넣지만, syntax·OA 지적별 판례 조회와 특징별 특허성 신호표는 아직 자동으로 붙이지 않는다. 지금은 오케스트레이터가 CLI로 붙이고, 웹에서는 사용자가 작업 도구의 판례 도구로 직접 본다.
- 판례 찾기의 후보 고르기는 글자 2-gram과 낱말 규칙뿐이다. 낱말이 전혀 겹치지 않는 같은 종류의 결함은 후보에 오르지 못해 Jev도 볼 수 없다.
- 특허성·권리행사 기록은 모델이 판결문을 읽고 뽑은 요약이다. 인용문은 코드로 원문과 대조했지만 유형·이유 분류는 사람이 검토하지 않았다.
- 2026-10-22 추출 결과: 830건 중 828건. 특허성 기록 1,128개(450건)와 권리행사 기록 513개(250건)다.
  - 추출 모델이 섞여 있다. 앞의 412건은 gemini-3.8-flash가 뽑았는데, 선불 크레딧이 소진되어 중단했다. 나머지 416건은 claude-sonnet-5-5가 뽑았다. 기록마다 `model` 칸에 어느 모델이 뽑았는지 남긴다.
  - 인용문을 원문에서 찾지 못해 버린 비율은 gemini 10.4%, Claude 2.3%였다.
  - 2건은 Claude가 세 번 모두 깨진 JSON을 돌려줘서 빠졌다. 그 답은 `graph/patent_case_law/extract/failed/`에 남는다.
- 권리행사 기록에서 W04(전제부 2개), W09(실시 주체 6개), W02·W05·W12(7~10개)는 적다. 시트와 점검표의 판례 근거도 그만큼 얇다.
