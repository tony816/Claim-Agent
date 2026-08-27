# 소스 역할 및 활성 순서

이 폴더에는 사용자가 승인한 프로젝트용 자료를 둔다. 승인되었다는 사실만으로 모든 파일이 현재 발명의 기술적 원자료이거나 검증된 긍정 예시가 되는 것은 아니다. 현재 발명의 원자료와 작성 기준, 후처리 기준, 검수 자료 및 예시 자료는 역할을 분리해 사용한다.

| 파일 | 역할 | 필수 소비·검증 역할 | 활성 시점 | 기술내용 근거로 사용 |
|---|---|---|---|---|
| `독립항_작성_성공조건.md` | 생성 방향·판정 기준 | claim-architect·claim-drafter; exact 문언은 claim-success-reviewer가 독립 재검사 | 독립항 작업 시작 전부터 최종 재검사까지 | 불가 |
| `07_용어표현_출처게이트.md` | 확정 기술 개념의 최종 용어·표현·띄어쓰기 출처 검증 | claim-style-adjuster; claim-success-reviewer·syntax-scope-reviewer가 기록 검증 | drafter의 PRE_STYLE 의미 초안 확정 후 | 불가 |
| `04_청구항_스타일가이드.md` | 문체·조사·분절·문장부호 후처리 | claim-style-adjuster; claim-success-reviewer·syntax-scope-reviewer가 기록 검증 | drafter의 PRE_STYLE 의미 초안 뒤 | 불가 |
| `청구항_예시검색_라우팅인덱스.md` | 국소 통사 문제용 검색 목차 | claim-style-adjuster가 조건부 하드 로딩; 사용 시 claim-success-reviewer가 기록 검증 | 기본 비활성, 특정 통사 문제가 남은 경우만 | 불가 |
| `청구항_문체학습용_분야별검색최적화본.md` | 최신 성공조건 미검증 역사 코퍼스 | claim-style-adjuster가 조건부 최소 문맥 로딩; 사용 시 claim-success-reviewer가 기록 검증 | 검증된 국소 조각 또는 확정 개념의 표면 표현 정확 검색 때만 | 불가 |
| `06_OA_심사리스크_체크리스트.md` | 잠정 문언 검수 + 정식 명세서 최종 검수 | oa-strategy-reviewer | 독립항 success·syntax 또는 종속항 dependent success·syntax를 통과한 동일 문언에만 | 불가 |
| `08_종속항_기술기여_게이트.md` | 종속항 후보의 과제–추가 기술특징–작동·협동 원리–효과 및 단순 도면 묘사 배제 검증 | dependent-claim-strategy-architect·claim-drafter; dependent success·syntax·OA reviewer가 검증 | DRAFT 또는 FINAL 독립항 LOCK 뒤, 종속항 트리·문언 작성 전 | 불가 |
| `05_종속항_전개패턴_가이드.md` | 기술기여 후보의 부모항·권리화 축·종속항 트리 설계 | dependent-claim-strategy-architect·claim-drafter; claim-success-reviewer·syntax-scope-reviewer가 독립 검증 | 후보별 세 기술기여 게이트 PASS 뒤부터 dependent syntax까지 | 불가 |

현재 발명의 명세서, 도면, 발명 설명, 구성요소 관계표, 선행기술 및 확정 문언은 별도의 발명 원자료다. 현재 이 폴더에 그러한 원자료가 없으면 예시 코퍼스나 가이드로 기술내용을 보충하지 않고 `근거 미확인` 또는 `BLOCK`으로 처리한다.

기본 활성 순서는 다음과 같다.

1. 생성 방향: 현재 발명 원자료 + `독립항_작성_성공조건.md`
2. 권리범위 결정: 성공조건의 조건 6(선행기술이 있을 때만) + 조건 8
3. `claim-drafter`의 보조 예시 없는 PRE_STYLE 의미 초안 작성
4. 별도 `claim-style-adjuster`의 용어·표현·스타일 후처리: `07_용어표현_출처게이트.md` + 조건 7 + `04_청구항_스타일가이드.md` + `CLAIM_STYLE_GATE`
5. 선택적 예시 검색: 해결되지 않은 국소 통사 문제 또는 확정 개념의 표면 표현 정확 검색이 필요한 경우에만 claim-style-adjuster가 라우팅 인덱스 전문과 역사 코퍼스의 정확한 조각·최소 문맥을 조건부로 읽음
6. 독립 success 재검사: 별도 claim-success-reviewer가 본 카탈로그, 최신 성공조건, 두 후처리 문서와 실제 원자료를 읽고 조건 1·4·2·3·9·8, CLAIM_STYLE_GATE, TERM_EXPRESSION_GATE, 범위 불변 및 조건 5 인계를 봉인
7. 통사·OA 보충 검수: success record가 PASS인 동일 문언에 syntax를 적용하고, 그 뒤 `06_OA_심사리스크_체크리스트.md`를 보충 적용
8. 종속항 기술기여 설계: DRAFT 또는 FINAL 독립항 LOCK 후 `08_종속항_기술기여_게이트.md`로 각 후보의 원자료 근거와 과제–특징–원리–효과를 검증하고 `DRAWING_ONLY`를 주된 세트에서 제외
9. 종속항 트리·문언 작성: 후보별 세 필수 기술기여 게이트 PASS 뒤 `05_종속항_전개패턴_가이드.md`로 부모항과 트리를 확정하여 `DEPENDENT_DESIGN_GATE`를 잠그고, drafter가 PRE_STYLE 문언화한 다음 style adjuster가 exact dependent revision을 확정
10. 종속항 success·후속 검수: claim-success-reviewer가 08·05와 exact 세트를 재검사해 dependent success를 봉인하고, syntax reviewer도 05를 직접 읽어 부모항·인용관계를 검수한 뒤 OA·종속항별 2단계 역구성과 `DEPENDENT_RECONSTRUCTION_GATE`를 거쳐 세트 LOCK 기록

원자료와 확정 문언은 사실상 기밀일 수 있다. 외부 서비스나 다른 프로젝트로 복사하기 전에 사용자의 범위 승인을 확인한다.
