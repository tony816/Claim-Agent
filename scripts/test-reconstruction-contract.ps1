[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptDirectory '..'))

function Read-ProjectFile([string]$RelativePath) {
    $path = Join-Path $projectRoot $RelativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing required file: $RelativePath"
    }
    return [System.IO.File]::ReadAllText($path)
}

function Assert-Contains([string]$RelativePath, [string[]]$Tokens) {
    $content = Read-ProjectFile $RelativePath
    foreach ($token in $Tokens) {
        if (-not $content.Contains($token)) {
            throw "$RelativePath is missing required contract token: $token"
        }
    }
}

Assert-Contains 'AGENTS.md' @(
    '후반 스타일 조정',
    'claim-style-adjuster',
    '성공조건 독립 검수',
    'claim-success-reviewer',
    'CLAIM_STYLE_GATE',
    '종속항별 블라인드 복원',
    'claim_scope: DEPENDENT_SINGLE',
    'DEPENDENT_RECONSTRUCTION_GATE'
)

Assert-Contains 'CLAUDE.md' @(
    'PRE_STYLE — NOT_GATE_ELIGIBLE',
    'style_record_id',
    'claim-success-reviewer',
    '종속항의 `dependent_success_record_id`는 이 역할만 발급',
    'CLAIM_STYLE_GATE',
    'NON_PATENT_TECHNICAL_READER_GATE',
    'GEOMETRIC_OBJECT_GATE',
    'DEPENDENT_RECONSTRUCTION_GATE',
    '각 목표 종속항마다 정확한 부모항 체인'
)

Assert-Contains '.claude\agents\claim-success-reviewer.md' @(
    'name: claim-success-reviewer',
    'success_scope: INDEPENDENT | DEPENDENT_SET',
    'sources/README.md',
    'sources/독립항_작성_성공조건.md',
    'success_record_id',
    'dependent_success_record_id',
    'syntax 진행 가능'
)

Assert-Contains '.claude\agents\claim-style-adjuster.md' @(
    'name: claim-style-adjuster',
    'STYLE_ONLY_REVISION',
    'CLAIM_STYLE_GATE',
    'FINALIZED_FOR_SUCCESS',
    '기술적 의미·한정 집합·명제 트리·권리범위',
    'sources/청구항_예시검색_라우팅인덱스.md',
    'sources/청구항_문체학습용_분야별검색최적화본.md',
    '조건부 보조 소스: NOT_ACTIVATED'
)

Assert-Contains '.claude\agents\claim-architect.md' @(
    'claim-style-adjuster에게 전달할 표면 문언 지시',
    'sources/README.md'
)

Assert-Contains '.claude\agents\dependent-claim-strategy-architect.md' @(
    'claim-style-adjuster에게 전달할 종속항 표면 문언',
    'sources/README.md'
)

Assert-Contains '.claude\agents\blind-claim-reconstruction-reviewer.md' @(
    'tools: []',
    'claim_scope: INDEPENDENT | DEPENDENT_SINGLE',
    'dependent_blind_snapshot_id',
    '1회독 도식화 가능성',
    '형상·공간 객체표'
)

Assert-Contains '.claude\agents\picture-claim-reconstruction-reviewer.md' @(
    'claim_scope: INDEPENDENT | DEPENDENT_SINGLE',
    'NON_PATENT_TECHNICAL_READER_GATE',
    'GEOMETRIC_OBJECT_GATE',
    'DEPENDENT_SINGLE'
)

Assert-Contains '.claude\agents\claim-drafter.md' @(
    'PRE_STYLE — NOT_GATE_ELIGIBLE',
    'claim-style-adjuster 인계 가능',
    '일반 기계 개발자',
    '기하 관찰용 단면이나 기준선이 실제 면·부품을 포함하는 주체가 되지 않게 한다',
    'PRE_STYLE_NON_PATENT_TECHNICAL_READER_CHECK'
)

Assert-Contains '.claude\agents\syntax-scope-reviewer.md' @(
    '스타일 기록 일치시험',
    'CLAIM_STYLE_GATE',
    '비특허 기술 독자 1회독 시험',
    '형상·공간 객체 귀속시험',
    '05_종속항_전개패턴_가이드.md'
)

Assert-Contains 'sources\04_청구항_스타일가이드.md' @(
    '전담 적용자는 `.claude/agents/claim-style-adjuster.md`',
    'CLAIM_STYLE_GATE',
    '단면·기준과 실제 형상 객체를 분리하는 경우',
    'A는 기준 방향과 직교하는 단면에서 오목면을 포함하는'
)

Assert-Contains 'sources\독립항_작성_성공조건.md' @(
    'drafter PRE_STYLE 의미 초안',
    'style_record_id',
    'CLAIM_STYLE_GATE',
    '.claude/agents/claim-success-reviewer.md'
)

Assert-Contains 'sources\05_종속항_전개패턴_가이드.md' @(
    'style_record_id',
    'CLAIM_STYLE_GATE',
    '별도 claim-style-adjuster',
    'claim-success-reviewer',
    'syntax-scope-reviewer'
)

Assert-Contains 'sources\README.md' @(
    '필수 소비·검증 역할',
    'claim-success-reviewer',
    'claim-style-adjuster가 조건부 하드 로딩',
    'syntax-scope-reviewer가 독립 검증'
)

Assert-Contains 'sources\청구항_예시검색_라우팅인덱스.md' @(
    'sources/청구항_예시검색_라우팅인덱스.md',
    'sources/청구항_문체학습용_분야별검색최적화본.md'
)

Assert-Contains 'sources\청구항_문체학습용_분야별검색최적화본.md' @(
    'sources/청구항_예시검색_라우팅인덱스.md',
    '정확한 문장 조각과 필요한 최소 문맥'
)

Assert-Contains 'web\PROJECT_INSTRUCTIONS.md' @(
    'bundle_version: 2026.08.27.5',
    'protocol_version: 1.4.0',
    'STYLE_PASS',
    'roles/claim-success-reviewer.md',
    'DEPENDENT_STYLE_PASS',
    'DEPENDENT_RECONSTRUCTION_PASS',
    'DEPENDENT_RECONSTRUCTION_GATE'
)

Assert-Contains 'web\BLIND_CHAT_PROMPT.md' @(
    'protocol_version: 1.4.0',
    'DEPENDENT_SINGLE 허용 입력 패킷'
)

Assert-Contains 'web\HANDOFF_TEMPLATES.md' @(
    'record_type: DESIGN | MEANING_DRAFT | STYLE',
    'dependent_style_record_id',
    'claim_style_gate: PASS',
    'style_change_table',
    'success_scope: INDEPENDENT | DEPENDENT_SET | N/A',
    'SUCCESS_REVIEW',
    'DEPENDENT_SUCCESS_REVIEW'
)

$claudeContent = Read-ProjectFile 'CLAUDE.md'
$styleIndex = $claudeContent.IndexOf('4. 별도 `claim-style-adjuster`', [System.StringComparison]::Ordinal)
$successIndex = $claudeContent.IndexOf('5. 별도 `claim-success-reviewer`', [System.StringComparison]::Ordinal)
$syntaxIndex = $claudeContent.IndexOf('6. 동일 식별자의 청구항 전문', [System.StringComparison]::Ordinal)
if ($styleIndex -lt 0 -or $successIndex -le $styleIndex -or $syntaxIndex -le $successIndex) {
    throw 'Independent drafter → style → success → syntax ordering is invalid in CLAUDE.md'
}

$dependentStyleIndex = $claudeContent.IndexOf('14. drafter의 DRAFTER_GATE가 PASS이면 별도 style adjuster', [System.StringComparison]::Ordinal)
$dependentSuccessIndex = $claudeContent.IndexOf('15. dependent style 결과가 PASS이면 별도 `claim-success-reviewer`', [System.StringComparison]::Ordinal)
$dependentSyntaxIndex = $claudeContent.IndexOf('16. `claim-success-reviewer`의 dependent success', [System.StringComparison]::Ordinal)
$dependentOaIndex = $claudeContent.IndexOf('17. 종속항 OA는', [System.StringComparison]::Ordinal)
$dependentBlindIndex = $claudeContent.IndexOf('18. `AUTHORING_DRAFT`에서는 CLAIM_STYLE_GATE', [System.StringComparison]::Ordinal)
$dependentLockIndex = $claudeContent.IndexOf('19. 동일 dependent_revision', [System.StringComparison]::Ordinal)
if ($dependentStyleIndex -lt 0 -or $dependentSuccessIndex -le $dependentStyleIndex -or $dependentSyntaxIndex -le $dependentSuccessIndex -or $dependentOaIndex -le $dependentSyntaxIndex) {
    throw 'Dependent drafter → style → success → syntax → OA ordering is invalid in CLAUDE.md'
}
if ($dependentOaIndex -lt 0 -or $dependentBlindIndex -le $dependentOaIndex -or $dependentLockIndex -le $dependentBlindIndex) {
    throw 'Dependent OA → reconstruction → LOCK ordering is invalid in CLAUDE.md'
}

$versionContent = Read-ProjectFile 'web\VERSION'
foreach ($expected in @(
    'bundle_version=2026.08.27.5',
    'protocol_version=1.4.0',
    'source_set_id=cc-web-2026.08.27.5'
)) {
    if (-not $versionContent.Contains($expected)) {
        throw "web/VERSION is missing: $expected"
    }
}

[pscustomobject]@{
    Status = 'PASS'
    Contract = 'source-ownership-success-and-reconstruction'
    BundleVersion = '2026.08.27.5'
    VerifiedFiles = 20
}
