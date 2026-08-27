[CmdletBinding()]
param(
    [string]$BackupName = '.claude.backup.zip'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptDirectory '..'))
$projectPrefix = $projectRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
$targetPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot '.claude.zip'))

if ([System.IO.Path]::GetFileName($BackupName) -ne $BackupName) {
    throw 'BackupName must be a file name without a directory.'
}

$backupPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $BackupName))
$stagingDirectory = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ('.tmp-claim-copa-archive-' + [guid]::NewGuid().ToString('N'))))
$newArchivePath = Join-Path $stagingDirectory '.claude.new.zip'

function Assert-WithinProject([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside project: $resolved"
    }
}

foreach ($path in @($targetPath, $backupPath, $stagingDirectory, $newArchivePath)) {
    Assert-WithinProject $path
}

if ($targetPath -eq $backupPath) {
    throw 'Backup path must differ from target path.'
}
if (Test-Path -LiteralPath $backupPath) {
    throw "Backup already exists: $backupPath"
}

try {
    New-Item -ItemType Directory -Path (Join-Path $stagingDirectory '.claude\agents') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $stagingDirectory 'sources') -Force | Out-Null

    Get-ChildItem -LiteralPath (Join-Path $projectRoot '.claude\agents') -File -Filter '*.md' |
        Copy-Item -Destination (Join-Path $stagingDirectory '.claude\agents')
    Get-ChildItem -LiteralPath (Join-Path $projectRoot 'sources') -File -Filter '*.md' |
        Copy-Item -Destination (Join-Path $stagingDirectory 'sources')

    foreach ($fileName in @('CLAUDE.md', 'README.md', '설정_개선보고서.md')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $fileName) -Destination $stagingDirectory
    }

    $stagedClaude = [System.IO.File]::ReadAllText((Join-Path $stagingDirectory 'CLAUDE.md'))
    $stagedBlindRole = [System.IO.File]::ReadAllText((Join-Path $stagingDirectory '.claude\agents\blind-claim-reconstruction-reviewer.md'))
    foreach ($requiredToken in @('DEPENDENT_RECONSTRUCTION_GATE', 'NON_PATENT_TECHNICAL_READER_GATE', 'GEOMETRIC_OBJECT_GATE')) {
        if (-not $stagedClaude.Contains($requiredToken)) {
            throw "CLAUDE.md is missing required reconstruction token: $requiredToken"
        }
    }
    foreach ($requiredToken in @('claim_scope: INDEPENDENT | DEPENDENT_SINGLE', 'dependent_blind_snapshot_id')) {
        if (-not $stagedBlindRole.Contains($requiredToken)) {
            throw "Blind reviewer is missing required reconstruction token: $requiredToken"
        }
    }

    $archiveInputs = @(
        (Join-Path $stagingDirectory '.claude'),
        (Join-Path $stagingDirectory 'sources'),
        (Join-Path $stagingDirectory 'CLAUDE.md'),
        (Join-Path $stagingDirectory 'README.md'),
        (Join-Path $stagingDirectory '설정_개선보고서.md')
    )
    Compress-Archive -Path $archiveInputs -DestinationPath $newArchivePath -CompressionLevel Optimal

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $probe = [System.IO.Compression.ZipFile]::OpenRead($newArchivePath)
    try {
        $entryNames = @($probe.Entries | ForEach-Object FullName)
        foreach ($requiredEntry in @(
            '.claude/agents/dependent-claim-strategy-architect.md',
            'sources/08_종속항_기술기여_게이트.md',
            'CLAUDE.md'
        )) {
            if ($entryNames -notcontains $requiredEntry) {
                throw "New archive is missing required entry: $requiredEntry"
            }
        }
    }
    finally {
        $probe.Dispose()
    }

    if (Test-Path -LiteralPath $targetPath) {
        [System.IO.File]::Replace($newArchivePath, $targetPath, $backupPath)
    }
    else {
        Move-Item -LiteralPath $newArchivePath -Destination $targetPath
    }

    $finalArchive = [System.IO.Compression.ZipFile]::OpenRead($targetPath)
    try {
        $finalEntries = @($finalArchive.Entries | ForEach-Object FullName)
        $entryCount = $finalEntries.Count
        $hasDependentRole = $finalEntries -contains '.claude/agents/dependent-claim-strategy-architect.md'
        $hasGate08 = $finalEntries -contains 'sources/08_종속항_기술기여_게이트.md'
    }
    finally {
        $finalArchive.Dispose()
    }

    [pscustomobject]@{
        Target = $targetPath
        Backup = if (Test-Path -LiteralPath $backupPath) { $backupPath } else { $null }
        EntryCount = $entryCount
        HasDependentRole = $hasDependentRole
        HasGate08 = $hasGate08
        TargetSize = (Get-Item -LiteralPath $targetPath).Length
    }
}
finally {
    $stagingLeaf = [System.IO.Path]::GetFileName($stagingDirectory)
    if ((Test-Path -LiteralPath $stagingDirectory) -and
        $stagingDirectory.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
        $stagingLeaf.StartsWith('.tmp-claim-copa-archive-', [System.StringComparison]::Ordinal)) {
        Remove-Item -LiteralPath $stagingDirectory -Recurse -Force
    }
}
