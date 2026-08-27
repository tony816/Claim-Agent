[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptDirectory '..'))
$webDirectory = Join-Path $projectRoot 'web'
$versionFile = Join-Path $webDirectory 'VERSION'

if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
    throw "VERSION file not found: $versionFile"
}

$versionValues = @{}
foreach ($line in [System.IO.File]::ReadAllLines($versionFile)) {
    if ($line -match '^([^=]+)=(.*)$') {
        $versionValues[$matches[1].Trim()] = $matches[2].Trim()
    }
}

foreach ($requiredKey in @('bundle_name', 'bundle_version', 'protocol_version', 'source_set_id')) {
    if (-not $versionValues.ContainsKey($requiredKey) -or [string]::IsNullOrWhiteSpace($versionValues[$requiredKey])) {
        throw "VERSION is missing required key: $requiredKey"
    }
}

$bundleName = $versionValues['bundle_name']
$bundleVersion = $versionValues['bundle_version']
$sourceSetId = $versionValues['source_set_id']
$distDirectory = Join-Path $projectRoot 'dist'
$bundleDirectory = Join-Path $distDirectory "$bundleName-$bundleVersion"
$zipPath = "$bundleDirectory.zip"

function Assert-WithinProject([string]$TargetPath) {
    $resolvedTarget = [System.IO.Path]::GetFullPath($TargetPath)
    $requiredPrefix = $projectRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedTarget.StartsWith($requiredPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside project: $resolvedTarget"
    }
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent)) {
        [System.IO.Directory]::CreateDirectory($parent) | Out-Null
    }
    [System.IO.File]::WriteAllText($Path, $Content, [System.Text.UTF8Encoding]::new($false))
}

function Copy-VersionedMarkdown([string]$SourcePath, [string]$DestinationPath, [string]$CanonicalPath) {
    $content = [System.IO.File]::ReadAllText($SourcePath)
    $header = "<!-- claim-copa-bundle: $bundleVersion; canonical-path: $CanonicalPath -->"
    if ($content -match '^<!-- claim-copa-bundle:') {
        $content = [System.Text.RegularExpressions.Regex]::Replace($content, '^<!-- claim-copa-bundle:[^\r\n]*-->\r?\n*', '')
    }
    Write-Utf8NoBom -Path $DestinationPath -Content "$header`r`n`r`n$content"
}

function Get-Sha256Text([string]$Content) {
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($Content)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

Assert-WithinProject $distDirectory
Assert-WithinProject $bundleDirectory
Assert-WithinProject $zipPath

if (-not (Test-Path -LiteralPath $distDirectory)) {
    [System.IO.Directory]::CreateDirectory($distDirectory) | Out-Null
}

if (Test-Path -LiteralPath $bundleDirectory) {
    Remove-Item -LiteralPath $bundleDirectory -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

[System.IO.Directory]::CreateDirectory($bundleDirectory) | Out-Null

foreach ($webFile in @('PROJECT_INSTRUCTIONS.md', 'README.md', 'BLIND_CHAT_PROMPT.md', 'HANDOFF_TEMPLATES.md')) {
    $sourcePath = Join-Path $webDirectory $webFile
    $destinationPath = Join-Path $bundleDirectory $webFile
    Copy-VersionedMarkdown -SourcePath $sourcePath -DestinationPath $destinationPath -CanonicalPath "web/$webFile"
}
Copy-Item -LiteralPath $versionFile -Destination (Join-Path $bundleDirectory 'VERSION')

$coreDirectory = Join-Path $bundleDirectory 'core'
[System.IO.Directory]::CreateDirectory($coreDirectory) | Out-Null
Copy-VersionedMarkdown -SourcePath (Join-Path $projectRoot 'CLAUDE.md') -DestinationPath (Join-Path $coreDirectory 'CLAUDE_CORE.md') -CanonicalPath 'CLAUDE.md'

$rolesDirectory = Join-Path $bundleDirectory 'roles'
[System.IO.Directory]::CreateDirectory($rolesDirectory) | Out-Null
Get-ChildItem -LiteralPath (Join-Path $projectRoot '.claude\agents') -Filter '*.md' -File | Sort-Object Name | ForEach-Object {
    Copy-VersionedMarkdown -SourcePath $_.FullName -DestinationPath (Join-Path $rolesDirectory $_.Name) -CanonicalPath ".claude/agents/$($_.Name)"
}

$sourcesDirectory = Join-Path $bundleDirectory 'sources'
[System.IO.Directory]::CreateDirectory($sourcesDirectory) | Out-Null
Get-ChildItem -LiteralPath (Join-Path $projectRoot 'sources') -Filter '*.md' -File | Sort-Object Name | ForEach-Object {
    Copy-VersionedMarkdown -SourcePath $_.FullName -DestinationPath (Join-Path $sourcesDirectory $_.Name) -CanonicalPath "sources/$($_.Name)"
}

$instructionsPath = Join-Path $bundleDirectory 'PROJECT_INSTRUCTIONS.md'
$instructions = [System.IO.File]::ReadAllText($instructionsPath)
foreach ($requiredToken in @($bundleVersion, $sourceSetId, 'WEB_SINGLE_CHAT', 'DRAFT-SELF', 'LOCK_MISSING_OR_STALE', 'STYLE_PASS', 'CLAIM_STYLE_GATE', 'DEPENDENT_STYLE_PASS', 'DEPENDENT_DESIGN_GATE', 'DEPENDENT_RECONSTRUCTION_GATE', 'DRAFT_DEPENDENT_SET_LOCK')) {
    if (-not $instructions.Contains($requiredToken)) {
        throw "PROJECT_INSTRUCTIONS is missing required token: $requiredToken"
    }
}

$fileCountBeforeManifests = (Get-ChildItem -LiteralPath $bundleDirectory -Recurse -File).Count
$contentFingerprintLines = [System.Collections.Generic.List[string]]::new()
Get-ChildItem -LiteralPath $bundleDirectory -Recurse -File | Sort-Object FullName | ForEach-Object {
    $relativePath = [System.IO.Path]::GetRelativePath($bundleDirectory, $_.FullName).Replace('\', '/')
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $contentFingerprintLines.Add("$relativePath`0$hash")
}
$sourceManifestDigest = Get-Sha256Text (($contentFingerprintLines -join "`n") + "`n")
$manifestSummary = @"
<!-- claim-copa-bundle: $bundleVersion -->

# Bundle manifest

- bundle_name: $bundleName
- bundle_version: $bundleVersion
- protocol_version: $($versionValues['protocol_version'])
- source_set_id: $sourceSetId
- source_manifest_digest: $sourceManifestDigest
- file_count_including_manifests: $($fileCountBeforeManifests + 2)
- integrity_file: MANIFEST.sha256

`MANIFEST.sha256` lists SHA-256 hashes for every packaged file except the hash list itself. The bundle version header on each Markdown file is the runtime version check used in web projects.
"@
Write-Utf8NoBom -Path (Join-Path $bundleDirectory 'BUNDLE_MANIFEST.md') -Content $manifestSummary

$manifestLines = [System.Collections.Generic.List[string]]::new()
Get-ChildItem -LiteralPath $bundleDirectory -Recurse -File | Sort-Object FullName | ForEach-Object {
    $relativePath = [System.IO.Path]::GetRelativePath($bundleDirectory, $_.FullName).Replace('\', '/')
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifestLines.Add("$hash  $relativePath")
}
Write-Utf8NoBom -Path (Join-Path $bundleDirectory 'MANIFEST.sha256') -Content (($manifestLines -join "`n") + "`n")

Compress-Archive -Path (Join-Path $bundleDirectory '*') -DestinationPath $zipPath -CompressionLevel Optimal

[pscustomobject]@{
    BundleDirectory = $bundleDirectory
    ZipPath = $zipPath
    BundleVersion = $bundleVersion
    SourceSetId = $sourceSetId
    SourceManifestDigest = $sourceManifestDigest
    FileCount = (Get-ChildItem -LiteralPath $bundleDirectory -Recurse -File).Count
}
