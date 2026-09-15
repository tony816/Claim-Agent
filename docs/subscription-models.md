# 구독 OAuth와 서브에이전트 모델 설정

## 화면에서 설정하기

1. `Claim-Agent.cmd`로 실행하고 왼쪽 **⚙ 에이전트 모델 설정**을 엽니다.
2. Claude Code 또는 Codex CLI가 없으면 해당 카드의 **CLI 설치 안내**를 따라 설치합니다. 설치 후 앱을 다시 실행합니다.
3. **구독 계정 연결**을 누르고 브라우저에서 본인 계정으로 로그인합니다. **연결 상태 확인**으로 완료 여부를 확인할 수 있습니다.
4. **기본 연결 방식**과 **기본 모델**을 고릅니다. 기본 설정은 일반 대화·요청 분류와 설정을 상속하는 역할에 적용됩니다.
5. 역할별로 연결 방식·모델·추론 강도를 고르고 **설정 저장**을 누릅니다. **전체에 기본 설정 적용**은 9개 역할의 제공자·모델을 기본 설정 상속으로 되돌립니다. 추론 강도는 유지합니다.

예를 들어 기본 연결은 Codex 구독으로 두고, 의미 초안은 Claude `opus`, 용어·스타일 조정은 Claude `sonnet`으로 설정할 수 있습니다. 기존 Gemini·Anthropic API 키 연결도 함께 사용할 수 있습니다.

`default`는 CLI가 계정 기본 모델을 선택한다는 뜻입니다. Codex 모델 선택기는 로컬 CLI 모델 목록 캐시의 공개 모델 ID도 읽어 표시합니다. 모델 선택기에 없는 ID는 **모델 ID 직접 입력**으로 지정합니다. 저장은 구문 검증이며 해당 계정의 모델 접근 권한이나 추론 강도 지원을 보장하지 않습니다. 접근 불가·한도 초과 시 해당 단계에서 오류를 보고하고 다른 제공자나 API 키로 자동 전환하지 않습니다.

## 인증·설정 저장

- OAuth는 공식 `claude auth login` / `codex login`으로 처리합니다. 앱이 인증 토큰을 읽거나 직접 API에 전송하지 않습니다.
- 로그인은 개인 CLI 세션과 분리된 `%USERPROFILE%\.claim-agent\auth\claude_oauth` 및 `codex_oauth` 저장소를 사용합니다. 따라서 다른 앱에 이미 로그인했더라도 이 화면에서 한 번 연결해야 합니다. 갱신과 보관은 공식 CLI가 담당합니다.
- 웹 모델 설정은 프로젝트의 `.tui/model-settings.json`에 저장되며 Git에서 제외됩니다. 토큰·비밀번호·API 키·실행 명령은 이 파일에 넣지 않습니다.
- 우선순위: `claim-agent.yaml` 또는 `--config` → 웹 모델 설정 → 실험/명령행 명시적 override. 웹 설정은 CLI 실행에도 적용됩니다.
- 실행 중인 웹 작업이 있으면 저장과 재로그인을 차단합니다. 저장한 설정은 다음 요청부터 적용되며, 기존 검수 문언·게이트·LOCK을 다시 발급하지 않습니다.

## YAML로 설정하기

```yaml
provider:
  kind: codex_oauth
  codex_oauth:
    model: default
    timeout_s: 600
    # executable: C:/tools/codex.exe
  claude_oauth:
    model: sonnet
    timeout_s: 600
    # executable: C:/Users/사용자/.local/bin/claude.exe
roles:
  claim-drafter:
    provider: claude_oauth
    model: opus
    thinking_level: HIGH
  claim-style-adjuster:
    provider: claude_oauth
    model: sonnet
    thinking_level: MEDIUM
```

`executable`은 실행 파일 경로이며 셸 명령을 넣을 수 없습니다. Windows 표준 npm 설치는 Node 엔트리포인트로 실행합니다. Codex는 `exec --ignore-user-config --ignore-rules --ephemeral`을 지원하는 CLI 버전, Claude는 `auth status --json`, `--setting-sources`, `--system-prompt-file`, `--effort` 등을 지원하는 CLI 버전이 필요합니다. 호환되지 않는 버전은 업데이트 후 연결하세요.

Claude를 앱 전용 폴더에 설치하려면 `npm install --prefix "%USERPROFILE%\.claim-agent\tools" @anthropic-ai/claude-code`를 실행할 수도 있습니다. 이 위치의 CLI는 실행기가 자동으로 찾습니다.

## 실행 계약

- 매 호출은 새 임시 작업 폴더에서 실행됩니다. 역할 지침은 별도 시스템 지침으로, 원자료·이전 단계 보고서는 기존 패킷 그대로 전달합니다. 긴 본문은 명령행이 아닌 stdin으로 전달합니다.
- 네이티브 파일·셸·MCP·서브에이전트 기능과 호스트 설정을 비활성화합니다. 블라인드는 이전 대화·원자료·이미지·도구·공통 캐시가 있으면 호출 전에 거부합니다.
- 도면은 Claude의 이미지 콘텐츠 블록 또는 Codex의 `--image`로 전달합니다. 일반 대화의 이전 이미지도 포함됩니다.
- 스타일 단계의 허용된 코퍼스 함수만 앱의 명시적 도구 요청 프로토콜로 실행합니다. 기존 후처리·성공조건·통사·OA·역복원 순서는 동일합니다.
- JSON 스키마를 본문에 전달하고 기존 파이프라인의 JSON 검증·repair 경로로 검수합니다. CLI 자체의 스키마 보장에 의존하지 않습니다.
- CLI 내부 캐시·sampling·출력 길이는 CLI/모델 정책에 따릅니다. 앱의 Gemini 캐시나 `temperature`를 그대로 적용하는 방식은 아닙니다. 역할별 추론 강도는 CLI의 effort 설정으로 전달합니다.
- 구독 호출은 토큰을 집계하되 API 단가를 구독 청구액으로 환산하지 않습니다. 비용은 미산정으로 기록합니다. 실제 사용량·추가 사용 요금은 공급자 계정에서 확인하세요.
- 웹에서 중지하면 Windows에서는 워커와 CLI 자식 프로세스를 함께 종료합니다. CLI 호출별 제한 시간도 적용합니다.

## 공식 근거 (2026-09-15 확인)

- [OpenAI 인증](https://learn.chatgpt.com/docs/auth): ChatGPT 구독 로그인, CLI 로그인·상태 확인·토큰 갱신.
- [Claude 구독과 Agent SDK](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan): 문서 상단의 6월 15일 업데이트에 따르면 별도 크레딧 전환은 보류되었으며 `claude -p`는 계속 구독 한도를 사용합니다. 아래쪽 과거 안내보다 상단 업데이트가 우선합니다.
- [Claude 비대화형 실행](https://code.claude.com/docs/en/headless), [CLI 옵션](https://code.claude.com/docs/en/cli-reference): stdin·이미지·JSON 응답·도구 제한. OAuth를 읽지 않는 `--bare` 모드는 구독 연결에서 사용하지 않습니다.

로그인 UI·모델 권한·사용량 제한은 공급자가 변경할 수 있습니다. 앱은 로그인 완료를 대신 승인하거나 추가 사용 결제를 활성화하지 않습니다.
