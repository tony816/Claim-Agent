# 판례 DB와 청구항 작성

- 작성일: 2026-10-22
- 코드: `claim_agent/graph/caselaw.py`, CLI `claim-agent graph caselaw …`

판례는 두 곳에 쓴다. 어느 쪽도 현재 발명의 원자료가 아니고 게이트 값을 바꾸지 않는다.

| 쓰는 곳 | 무엇 | 받는 역할 |
|---|---|---|
| 작성 전 주의 시트 | `sources/11_판례_청구항_주의시트.md` (S11) — 법원이 불명확·불일치·좁게 해석한 청구항 문언 유형 | claim-architect, claim-drafter (success reviewer는 사용 경계 감사만) |
| 검수 지적의 참고 근거 | syntax·OA 지적마다 같은 종류의 결함을 판단한 판례 | 오케스트레이터가 지적에 붙여 사용자에게 보고 |

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

## 한계

- 쟁점·결정 사실은 모델 요약이고 사람 법리 검토 전이다.
- 조회 대상은 교훈 기록 352개(판례 274건)다. DB 전체 7,739청크 전문 검색은 아직 없다.
- 엔진(`claim-agent run`)은 S11을 패킷에 넣지만, syntax·OA 지적에 판례를 자동으로 붙이지는 않는다. 지금은 오케스트레이터가 CLI로 붙인다.
