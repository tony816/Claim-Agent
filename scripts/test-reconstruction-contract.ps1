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
    '종속항별 블라인드 복원',
    'claim_scope: DEPENDENT_SINGLE',
    'DEPENDENT_RECONSTRUCTION_GATE'
)

Assert-Contains 'CLAUDE.md' @(
    'NON_PATENT_TECHNICAL_READER_GATE',
    'GEOMETRIC_OBJECT_GATE',
    'DEPENDENT_RECONSTRUCTION_GATE',
    '각 목표 종속항마다 정확한 부모항 체인'
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
    '일반 기계 개발자',
    '기하 관찰용 단면이나 기준선은 관찰·측정 기준',
    'NON_PATENT_TECHNICAL_READER_GATE'
)

Assert-Contains '.claude\agents\syntax-scope-reviewer.md' @(
    '비특허 기술 독자 1회독 시험',
    '형상·공간 객체 귀속시험'
)

Assert-Contains 'sources\04_청구항_스타일가이드.md' @(
    '단면·기준과 실제 형상 객체를 분리하는 경우',
    'A는 기준 방향과 직교하는 단면에서 오목면을 포함하는'
)

Assert-Contains 'web\PROJECT_INSTRUCTIONS.md' @(
    'bundle_version: 2026.08.27.3',
    'protocol_version: 1.2.0',
    'DEPENDENT_RECONSTRUCTION_PASS',
    'DEPENDENT_RECONSTRUCTION_GATE'
)

Assert-Contains 'web\BLIND_CHAT_PROMPT.md' @(
    'protocol_version: 1.2.0',
    'DEPENDENT_SINGLE 허용 입력 패킷'
)

$claudeContent = Read-ProjectFile 'CLAUDE.md'
$dependentOaIndex = $claudeContent.IndexOf('13. 종속항 OA는', [System.StringComparison]::Ordinal)
$dependentBlindIndex = $claudeContent.IndexOf('14. `AUTHORING_DRAFT`에서는 dependent success', [System.StringComparison]::Ordinal)
$dependentLockIndex = $claudeContent.IndexOf('15. 동일 dependent_revision', [System.StringComparison]::Ordinal)
if ($dependentOaIndex -lt 0 -or $dependentBlindIndex -le $dependentOaIndex -or $dependentLockIndex -le $dependentBlindIndex) {
    throw 'Dependent OA → reconstruction → LOCK ordering is invalid in CLAUDE.md'
}

$versionContent = Read-ProjectFile 'web\VERSION'
foreach ($expected in @(
    'bundle_version=2026.08.27.3',
    'protocol_version=1.2.0',
    'source_set_id=cc-web-2026.08.27.3'
)) {
    if (-not $versionContent.Contains($expected)) {
        throw "web/VERSION is missing: $expected"
    }
}

[pscustomobject]@{
    Status = 'PASS'
    Contract = 'dependent-claim-reader-and-geometric-reconstruction'
    BundleVersion = '2026.08.27.3'
    VerifiedFiles = 10
}
