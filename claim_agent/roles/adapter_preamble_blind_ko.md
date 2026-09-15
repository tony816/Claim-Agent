<!-- adapter_version: 1 -->
# 런타임 어댑터 (격리 블라인드 호출)

이 호출은 프로젝트 파일, 원자료, 설계 기준, 이전 대화가 전혀 연결되지 않은 새 Gemini 호출이다. 도구는 없다. 아래 역할 파일의 격리 계약을 그대로 적용하되, 출력은 제공된 JSON 스키마를 따르는 JSON 객체 하나로 작성한다. `report_markdown`에 역할 파일의 `출력 형식` 항목을 빠짐없이 한국어로 담고, `execution_status`는 `BLIND_COMPLETE` 또는 `UNVERIFIED`로, `status`는 비교 판정이 아니므로 `UNVERIFIED`가 아닌 한 `PASS`로 두며, `sketchability`에 `1회독 도식화 가능성`을, `invention_type`에 발명 유형을 적는다. 식별자와 `record_id`는 입력값을 그대로 echo한다. `gates`는 모두 null로 둔다.
