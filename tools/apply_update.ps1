param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDir,
    [Parameter(Mandatory = $true)]
    [string]$PackagePath,
    [Parameter(Mandatory = $true)]
    [int]$LauncherPid,
    [Parameter(Mandatory = $true)]
    [string]$RestartExecutable,
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Resolve-ExistingPath([string]$Value, [string]$Kind) {
    $resolved = [System.IO.Path]::GetFullPath($Value)
    if ($Kind -eq "Directory" -and -not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "Directory not found: $resolved"
    }
    if ($Kind -eq "File" -and -not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "File not found: $resolved"
    }
    return $resolved
}

function Wait-FileReadyForReplacement(
    [string]$Path,
    [int]$TimeoutSeconds = 45
) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
            $stream.Dispose()
            return
        }
        catch {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "File remained in use after ${TimeoutSeconds}s: $Path"
            }
            Start-Sleep -Milliseconds 200
        }
    } while ($true)
}

function Copy-FileWithRetry(
    [string]$Source,
    [string]$Destination,
    [int]$TimeoutSeconds = 15
) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            Copy-Item -LiteralPath $Source -Destination $Destination -Force
            return
        }
        catch {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw
            }
            Start-Sleep -Milliseconds 200
        }
    } while ($true)
}

function Test-AllowedUpdatePath([string]$RelativePath) {
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or
        $RelativePath.Contains("\") -or
        $RelativePath.StartsWith("/") -or
        $RelativePath.Split("/") -contains "..") {
        return $false
    }

    $lower = $RelativePath.ToLowerInvariant()
    $parts = $lower.Split("/")
    $forbiddenParts = @(
        "cache_ios", "cache_android", "guest_saves", "save_backups",
        "logs", "run", "__pycache__"
    )
    $forbiddenNames = @(
        "local_settings.json", "device_links.json", "whitelist.json",
        "fixed_manifest.json", "fixed_manifest_ios.json",
        "fixed_manifest_android.json", "save_sessions.json",
        "save_session_release_requests.json", "jpb-server.pid",
        "jpb-backup.pid", "dino-dns.pid"
    )
    foreach ($part in $parts) {
        if ($forbiddenParts -contains $part) {
            return $false
        }
    }
    if ($forbiddenNames -contains $parts[-1]) {
        return $false
    }

    $rootFiles = @(
        "dinoserver.exe", "server_current.py", "backup_guest_saves.py",
        "readme_first.txt", "local_server_readme.txt", "cache_setup.txt",
        "third_party_notices.md", "changelog.md", "open_lan_firewall.bat"
    )
    if ($rootFiles -contains $lower) {
        return $true
    }
    if (@(
        "config/cache_index_ios.json",
        "config/cache_index_android.json",
        "config/onlineoptions",
        "config/offer_rotation.json"
    ) -contains $lower) {
        return $true
    }

    $extension = [System.IO.Path]::GetExtension($lower)
    if ($lower.StartsWith("launcher/") -and $extension -eq ".py") { return $true }
    if ($lower.StartsWith("jpb_server/") -and $extension -eq ".py") { return $true }
    if ($lower.StartsWith("tools/") -and @(".py", ".ps1") -contains $extension) { return $true }
    if ($lower.StartsWith("assets/guide/") -and @(".png", ".jpg", ".jpeg", ".webp") -contains $extension) { return $true }
    if ($lower.StartsWith("assets/icons/") -and @(".png", ".svg", ".txt") -contains $extension) { return $true }
    if ($lower.StartsWith("icons/") -and @(".png", ".ico") -contains $extension) { return $true }
    return $false
}

$install = Resolve-ExistingPath $InstallDir "Directory"
$package = Resolve-ExistingPath $PackagePath "File"
$restart = [System.IO.Path]::GetFullPath($RestartExecutable)
if (-not $restart.StartsWith(
    $install.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
        [System.IO.Path]::DirectorySeparatorChar,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Restart executable must be inside the Dino Server folder."
}
$errorLog = Join-Path $install "logs\update_error.log"

$workRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "DinoServer-Apply-" + [Guid]::NewGuid().ToString("N")
)
$extractRoot = Join-Path $workRoot "extract"
$backupRoot = Join-Path $workRoot "backup"
$newFiles = [System.Collections.Generic.List[string]]::new()
$backedUp = [System.Collections.Generic.List[string]]::new()
$updated = $false

try {
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $deadline) {
        $process = Get-Process -Id $LauncherPid -ErrorAction SilentlyContinue
        if (-not $process) {
            break
        }
        Start-Sleep -Milliseconds 150
    }
    if (Get-Process -Id $LauncherPid -ErrorAction SilentlyContinue) {
        throw "Dino Server did not close before the update timeout."
    }
    # PyInstaller one-file builds briefly keep the public EXE open in a
    # parent bootloader process after the Python child has exited. Waiting
    # only for LauncherPid can therefore race with replacement of the EXE.
    Wait-FileReadyForReplacement -Path $restart -TimeoutSeconds 45

    New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    [System.IO.Compression.ZipFile]::ExtractToDirectory($package, $extractRoot)

    $updateRoot = Join-Path $extractRoot "DinoServerUpdate"
    $payloadRoot = Join-Path $updateRoot "payload"
    $manifestPath = Join-Path $updateRoot "update_manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $payloadRoot -PathType Container)) {
        throw "The downloaded update layout is invalid."
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if (-not $manifest.files) {
        throw "The downloaded update manifest has no files."
    }

    foreach ($entry in $manifest.files) {
        $relative = [string]$entry.path
        if (-not (Test-AllowedUpdatePath $relative)) {
            throw "Forbidden update path: $relative"
        }
        $source = [System.IO.Path]::GetFullPath(
            (Join-Path $payloadRoot ($relative.Replace("/", "\")))
        )
        $payloadPrefix = $payloadRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
            [System.IO.Path]::DirectorySeparatorChar
        if (-not $source.StartsWith(
            $payloadPrefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Unsafe update source path: $relative"
        }
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Missing update file: $relative"
        }
        $actualHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
        if ($actualHash -ne ([string]$entry.sha256)) {
            throw "Update file failed SHA-256 verification: $relative"
        }
    }

    foreach ($entry in $manifest.files) {
        $relative = [string]$entry.path
        $windowsRelative = $relative.Replace("/", "\")
        $source = Join-Path $payloadRoot $windowsRelative
        $destination = [System.IO.Path]::GetFullPath(
            (Join-Path $install $windowsRelative)
        )
        $installPrefix = $install.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
            [System.IO.Path]::DirectorySeparatorChar
        if (-not $destination.StartsWith(
            $installPrefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Unsafe update destination path: $relative"
        }
        $destinationParent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            $backup = Join-Path $backupRoot $windowsRelative
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force |
                Out-Null
            Copy-Item -LiteralPath $destination -Destination $backup -Force
            $backedUp.Add($relative)
        }
        else {
            $newFiles.Add($relative)
        }
        Copy-FileWithRetry -Source $source -Destination $destination
    }
    $updated = $true
}
catch {
    $originalError = $_
    $rollbackErrors = [System.Collections.Generic.List[string]]::new()
    foreach ($relative in $newFiles) {
        $destination = Join-Path $install ($relative.Replace("/", "\"))
        try {
            Remove-Item -LiteralPath $destination -Force -ErrorAction Stop
        }
        catch {
            $rollbackErrors.Add("$relative`: $($_.Exception.Message)")
        }
    }
    foreach ($relative in $backedUp) {
        $backup = Join-Path $backupRoot ($relative.Replace("/", "\"))
        $destination = Join-Path $install ($relative.Replace("/", "\"))
        if (Test-Path -LiteralPath $backup -PathType Leaf) {
            try {
                Copy-FileWithRetry -Source $backup -Destination $destination
            }
            catch {
                $rollbackErrors.Add("$relative`: $($_.Exception.Message)")
            }
        }
    }
    $failureMessage = [string]$originalError.Exception.Message
    if ($rollbackErrors.Count -gt 0) {
        $failureMessage += "`r`nRollback errors: " + ($rollbackErrors -join " | ")
    }
    try {
        New-Item -ItemType Directory -Path (Split-Path -Parent $errorLog) -Force |
            Out-Null
        Add-Content -LiteralPath $errorLog -Encoding UTF8 -Value (
            "[$([DateTime]::Now.ToString('s'))] $failureMessage"
        )
    }
    catch {
    }
    if (-not $NoRestart) {
        try {
            Add-Type -AssemblyName PresentationFramework
            [System.Windows.MessageBox]::Show(
                "The update failed and the previous Dino Server files were restored.`n`n$failureMessage`n`nDetails: $errorLog",
                "Dino Server Update",
                "OK",
                "Error"
            ) | Out-Null
        }
        catch {
        }
    }
}
finally {
    if (-not $NoRestart -and (Test-Path -LiteralPath $restart -PathType Leaf)) {
        # The helper inherits PyInstaller's private _PYI_* environment from
        # the launcher. Starting another one-file EXE with that environment
        # makes it reuse the old _MEI directory while the old bootloader is
        # deleting it, which produces "Failed to load Python DLL".
        Get-ChildItem Env: |
            Where-Object { $_.Name -like "_PYI_*" } |
            ForEach-Object {
                Remove-Item -LiteralPath ("Env:" + $_.Name) -ErrorAction SilentlyContinue
            }
        $env:PYINSTALLER_RESET_ENVIRONMENT = "1"
        Start-Process -FilePath $restart -WorkingDirectory $install
    }
    if ($updated) {
        Remove-Item -LiteralPath $package -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $backupRoot -Recurse -Force -ErrorAction SilentlyContinue
}
