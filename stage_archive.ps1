$Source = "Y:\LiHua"
$TapeId = "MB2FEQ"
$ArchiveRoot = "D:\"

# Robocopy multi-thread count.
$RoboThreads = 14

# Keep some free space on D so the staging disk is not filled completely.
$ReserveFreeGB = 5

# Maximum number of file names passed to one Robocopy call.
# Files are first grouped by their source directory, then chunked.
$MaxFilesPerRoboCall = 80

$ErrorActionPreference = "Stop"

# Version: MT14 tar-worker-fixed


# ============================================================
# Paths
# ============================================================

$Owner = Split-Path -Path $Source -Leaf
$Target = Join-Path $ArchiveRoot "${Owner}_archive"
$RawTarget = Join-Path $Target $Owner

$IndexDir = Join-Path $ArchiveRoot "Archive_Index"
$ProgressPath = Join-Path $IndexDir "archive_files.csv"
$ArchiveIndexPath = Join-Path $IndexDir "archive_index.csv"

$ArchiveDriveName = $ArchiveRoot.Substring(0, 1)
[Int64]$ReserveFreeBytes = [Int64]($ReserveFreeGB * 1GB)


# ============================================================
# Functions
# ============================================================

function Save-Progress {
    param(
        [object[]]$Rows
    )

    $Rows = @($Rows)

    if ($Rows.Count -eq 0) {
        Remove-Item -LiteralPath $ProgressPath -Force -ErrorAction SilentlyContinue
        return
    }

    $TempPath = "$ProgressPath.tmp"

    $Rows |
        Select-Object owner, relative_path, size_bytes, type, batch_id, status, is_final_batch |
        Export-Csv -LiteralPath $TempPath -NoTypeInformation -Encoding UTF8

    Move-Item -LiteralPath $TempPath -Destination $ProgressPath -Force
}


function New-ProgressRow {
    param(
        [string]$RelativePath,
        [Int64]$SizeBytes,
        [string]$Type
    )

    [PSCustomObject]@{
        owner          = $Owner
        relative_path  = $RelativePath
        size_bytes     = $SizeBytes
        type           = $Type
        batch_id       = $BatchId
        status         = "STAGED"
        is_final_batch = $(if ($IsFinalBatch) { "1" } else { "0" })
    }
}


function Get-FreeBytes {
    return [Int64](Get-PSDrive -Name $ArchiveDriveName).Free
}


function Get-FreeGB {
    return [math]::Round((Get-FreeBytes) / 1GB, 2)
}


function Split-ItemsIntoChunks {
    param(
        [object[]]$Items,
        [int]$ChunkSize
    )

    $Items = @($Items)

    if ($Items.Count -eq 0) {
        return
    }

    for ($i = 0; $i -lt $Items.Count; $i += $ChunkSize) {
        $End = [math]::Min($i + $ChunkSize - 1, $Items.Count - 1)
        ,@($Items[$i..$End])
    }
}


# ============================================================
# Initialize index directory
# ============================================================

New-Item -ItemType Directory -Force -Path $IndexDir | Out-Null


# ============================================================
# Safety: do not restart an owner that already has a completed
# archive_index.csv record.
# ============================================================

if (Test-Path -LiteralPath $ArchiveIndexPath) {
    $FinishedRecords = @(
        Import-Csv -LiteralPath $ArchiveIndexPath |
            Where-Object { $_.owner -eq $Owner }
    )

    if ($FinishedRecords.Count -gt 0) {
        Write-Host ""
        Write-Host "This owner already has a completed archive record:" -ForegroundColor Yellow
        Write-Host "Owner : $Owner"
        Write-Host "Index : $ArchiveIndexPath"
        Write-Host "Nothing was staged."
        Write-Host ""
        pause
        exit 0
    }
}


# ============================================================
# Load progress CSV
# ============================================================

$ProgressRows = @()

if (Test-Path -LiteralPath $ProgressPath) {
    $ProgressRows = @(Import-Csv -LiteralPath $ProgressPath)
}

$OwnerRows = @(
    $ProgressRows |
        Where-Object { $_.owner -eq $Owner }
)

$StagedRows = @(
    $OwnerRows |
        Where-Object { $_.status -eq "STAGED" }
)


# ============================================================
# If a previous batch is still STAGED, it must go to tape first.
# ============================================================

if ($StagedRows.Count -gt 0) {
    Write-Host ""
    Write-Host "A staged batch is waiting for tape backup: $Owner" -ForegroundColor Yellow
    Write-Host "Run copy_to_tape.ps1 first."
    Write-Host ""
    pause
    exit 0
}


# ============================================================
# Scan source
# ============================================================

if (-not (Test-Path -LiteralPath $Source)) {
    Write-Host "Source does not exist: $Source" -ForegroundColor Red
    pause
    exit 1
}

Write-Host "Scanning: $Source"

$AllFiles = @(
    Get-ChildItem -LiteralPath $Source -Recurse -File -Force
)

$SourceRaw = @(
    $AllFiles |
        Where-Object { $_.Extension -ieq ".raw" }
)

$SourceNonRaw = @(
    $AllFiles |
        Where-Object { $_.Extension -ine ".raw" }
)


# ============================================================
# Determine RAW files that are already ARCHIVED on tape.
# ============================================================

$ArchivedRawPaths = @{}

foreach (
    $Row in (
        $OwnerRows |
            Where-Object {
                $_.status -eq "ARCHIVED" -and
                $_.type -eq "raw"
            }
    )
) {
    if (-not [string]::IsNullOrWhiteSpace($Row.relative_path)) {
        $ArchivedRawPaths[$Row.relative_path.ToLowerInvariant()] = $true
    }
}


# ============================================================
# PendingRaw = RAW files that are NOT yet ARCHIVED.
# ============================================================

$PendingRaw = @(
    foreach ($File in $SourceRaw) {
        $RelativePath = $File.FullName.Substring($Source.Length).TrimStart('\')

        if (-not $ArchivedRawPaths.ContainsKey($RelativePath.ToLowerInvariant())) {
            [PSCustomObject]@{
                File         = $File
                RelativePath = $RelativePath
            }
        }
    }
)

$NonRawAlreadyArchived = @(
    $OwnerRows |
        Where-Object {
            $_.status -eq "ARCHIVED" -and
            $_.type -eq "nonraw"
        }
).Count -gt 0


Write-Host "RAW total   : $($SourceRaw.Count)"
Write-Host "RAW archived: $($ArchivedRawPaths.Count)"
Write-Host "RAW pending : $($PendingRaw.Count)"


# ============================================================
# Clean local files that the CSV explicitly says are ARCHIVED.
# This handles "tape succeeded but local cleanup was interrupted".
# ============================================================

if ($ArchivedRawPaths.Count -gt 0 -and (Test-Path -LiteralPath $RawTarget)) {
    foreach (
        $Row in (
            $OwnerRows |
                Where-Object {
                    $_.status -eq "ARCHIVED" -and
                    $_.type -eq "raw"
                }
        )
    ) {
        $ArchivedLocalFile = Join-Path $RawTarget $Row.relative_path

        if (Test-Path -LiteralPath $ArchivedLocalFile) {
            Write-Host "Removing retained ARCHIVED RAW from D: $($Row.relative_path)"
            Remove-Item -LiteralPath $ArchivedLocalFile -Force -ErrorAction Stop
        }
    }
}

if ($NonRawAlreadyArchived -and (Test-Path -LiteralPath $Target)) {
    Get-ChildItem -LiteralPath $Target -File -Filter "${Owner}_nonraw*.tar" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Write-Host "Removing retained ARCHIVED tar from D: $($_.Name)"
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
        }
}


# ============================================================
# NON-RAW TAR
# ============================================================

$TarReady = $false
$TarPath = $null
$TarName = $null

if ($SourceNonRaw.Count -gt 0 -and -not $NonRawAlreadyArchived) {

    New-Item -ItemType Directory -Force -Path $Target | Out-Null

    $ExistingTar = @(
        Get-ChildItem -LiteralPath $Target -File -Filter "${Owner}_nonraw*.tar" `
            -ErrorAction SilentlyContinue |
            Select-Object -First 1
    )

    if ($ExistingTar.Count -gt 0) {
        $TarName = $ExistingTar[0].Name
    }
    else {
        $TarName = "${Owner}_nonraw_$(Get-Date -Format 'yyyyMMdd').tar"
    }

    $TarPath = Join-Path $Target $TarName


    # --------------------------------------------------------
    # Reuse an existing complete tar if one is already present.
    # --------------------------------------------------------

    if (Test-Path -LiteralPath $TarPath) {
        Write-Host "Checking existing tar: $TarPath"

        & tar.exe -tf $TarPath > $null 2>&1
        $ExistingTarVerifyCode = $LASTEXITCODE

        if ($ExistingTarVerifyCode -eq 0) {
            Write-Host "Using existing complete tar: $TarPath" -ForegroundColor Green
            $TarReady = $true
        }
        else {
            Write-Host "Existing tar is incomplete or invalid. Removing it." -ForegroundColor Yellow
            Remove-Item -LiteralPath $TarPath -Force -ErrorAction Stop
        }
    }


    # --------------------------------------------------------
    # Before creating a new tar, make sure it should fit.
    # Tar size is close to the total non-RAW source size, so use
    # that as a conservative pre-check plus the reserve.
    # --------------------------------------------------------

    if (-not $TarReady) {
        [Int64]$NonRawSourceBytes = 0

        foreach ($File in $SourceNonRaw) {
            $NonRawSourceBytes += [Int64]$File.Length
        }

        [Int64]$TarRequiredBytes = $NonRawSourceBytes + $ReserveFreeBytes
        [Int64]$TarFreeBytes = Get-FreeBytes

        if ($TarFreeBytes -lt $TarRequiredBytes) {
            Write-Host ""
            Write-Host "Not enough D-drive space to safely create the non-RAW tar." -ForegroundColor Red
            Write-Host "Non-RAW source size: $([math]::Round($NonRawSourceBytes / 1GB, 2)) GB"
            Write-Host "D free             : $([math]::Round($TarFreeBytes / 1GB, 2)) GB"
            Write-Host "Reserved           : $ReserveFreeGB GB"
            Write-Host ""
            pause
            exit 1
        }


        Write-Host ""
        Write-Host "Creating non-RAW tar: $TarPath"
        Write-Host ""

        $SourceParent = Split-Path -Path $Source -Parent

        # ====================================================
        # TAR WORKER FIX
        #
        # Do not pass tar.exe arguments through a manually
        # concatenated ProcessStartInfo.Arguments string.
        # Windows PowerShell 5.1 may misparse/drop the final
        # source directory argument.
        #
        # Instead, create a tiny temporary PowerShell worker
        # that runs:
        #     & tar.exe @TarArguments
        #
        # The actual paths are passed through environment
        # variables, so spaces / Chinese characters do not
        # need extra command-line escaping.
        # ====================================================

        $TarWorkerPath = Join-Path $IndexDir "${Owner}_tar_worker.ps1"

        $TarWorkerContent = @'
$ErrorActionPreference = "Stop"

$TarArguments = @(
    "-cf"
    $env:ARCHIVE_TAR_PATH
    "--exclude=*.[rR][aA][wW]"
    "-C"
    $env:ARCHIVE_SOURCE_PARENT
    $env:ARCHIVE_OWNER
)

& tar.exe @TarArguments
exit $LASTEXITCODE
'@

        Set-Content `
            -LiteralPath $TarWorkerPath `
            -Value $TarWorkerContent `
            -Encoding UTF8

        $ProcessInfo = New-Object System.Diagnostics.ProcessStartInfo
        $ProcessInfo.FileName = "powershell.exe"
        $ProcessInfo.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$TarWorkerPath`""
        $ProcessInfo.UseShellExecute = $false
        $ProcessInfo.CreateNoWindow = $false

        $ProcessInfo.EnvironmentVariables["ARCHIVE_TAR_PATH"] = $TarPath
        $ProcessInfo.EnvironmentVariables["ARCHIVE_SOURCE_PARENT"] = $SourceParent
        $ProcessInfo.EnvironmentVariables["ARCHIVE_OWNER"] = $Owner

        $TarProcess = New-Object System.Diagnostics.Process
        $TarProcess.StartInfo = $ProcessInfo

        $StartTime = Get-Date
        Write-Host "tar starting..."

        try {
            $Started = $TarProcess.Start()

            if (-not $Started) {
                throw "tar worker process could not be started."
            }

            while (-not $TarProcess.WaitForExit(60000)) {
                $Elapsed = (Get-Date) - $StartTime

                if (Test-Path -LiteralPath $TarPath) {
                    $TarFile = Get-Item -LiteralPath $TarPath
                    $TarGB = [math]::Round($TarFile.Length / 1GB, 2)
                    $FreeGB = Get-FreeGB

                    Write-Host (
                        "tar writing: {0} GB | elapsed: {1:hh\:mm\:ss} | D free: {2} GB" `
                            -f $TarGB, $Elapsed, $FreeGB
                    )
                }
                else {
                    Write-Host (
                        "tar starting... | elapsed: {0:hh\:mm\:ss}" `
                            -f $Elapsed
                    )
                }
            }

            $TarProcess.WaitForExit()
            $TarExitCode = $TarProcess.ExitCode
        }
        catch {
            Write-Host ""
            Write-Host "Failed to run tar worker." -ForegroundColor Red
            Write-Host $_.Exception.Message
            Write-Host ""

            if ($null -ne $TarProcess) {
                $TarProcess.Dispose()
            }

            Remove-Item `
                -LiteralPath $TarWorkerPath `
                -Force `
                -ErrorAction SilentlyContinue

            pause
            exit 1
        }

        Remove-Item `
            -LiteralPath $TarWorkerPath `
            -Force `
            -ErrorAction SilentlyContinue

        $Elapsed = (Get-Date) - $StartTime

        Write-Host ""
        Write-Host "tar finished."
        Write-Host ("Elapsed: {0:hh\:mm\:ss}" -f $Elapsed)
        Write-Host "tar exit code: $TarExitCode"

        if (Test-Path -LiteralPath $TarPath) {
            $FinalTarGB = [math]::Round((Get-Item -LiteralPath $TarPath).Length / 1GB, 2)
            Write-Host "Final tar size: $FinalTarGB GB"
        }

        if ($TarExitCode -ne 0) {
            Write-Host ""
            Write-Host "TAR CREATION FAILED" -ForegroundColor Red
            Write-Host "Exit code: $TarExitCode"
            Write-Host "Check the tar.exe error shown above."
            Write-Host ""

            $TarProcess.Dispose()

            pause
            exit 1
        }

        Write-Host "Verifying tar archive..."

        & tar.exe -tf $TarPath > $null 2>&1
        $TarVerifyCode = $LASTEXITCODE

        if ($TarVerifyCode -ne 0) {
            Write-Host ""
            Write-Host "Tar verification failed." -ForegroundColor Red
            Write-Host "Verification exit code: $TarVerifyCode"
            Write-Host ""

            $TarProcess.Dispose()

            pause
            exit 1
        }

        Write-Host "Tar created and verified successfully." -ForegroundColor Green
        Write-Host ""

        $TarReady = $true
        $TarProcess.Dispose()
    }
}


# ============================================================
# Select the RAW files for THIS staging batch.
#
# Important:
# - Existing complete pending files on D cost no additional space,
#   so they are always included.
# - New RAW files are added until D free space - reserve is used.
# - No .rcj and no /XF are used.
# ============================================================

$BatchRaw = @()
$ExistingCompleteRaw = @()
$NewRawToCopy = @()

[Int64]$FreeBytesForRaw = Get-FreeBytes
[Int64]$AvailableForNewRaw = $FreeBytesForRaw - $ReserveFreeBytes

if ($AvailableForNewRaw -lt 0) {
    $AvailableForNewRaw = 0
}

[Int64]$PlannedNewBytes = 0
$SpaceLimitReached = $false


foreach ($Item in $PendingRaw) {

    $DestinationFile = Join-Path $RawTarget $Item.RelativePath

    # --------------------------------------------------------
    # Recover a complete file left on D by a previous interrupted
    # stage run. It is pending in CSV, so it is safe to include.
    # --------------------------------------------------------

    if (Test-Path -LiteralPath $DestinationFile) {

        $ExistingSize = (Get-Item -LiteralPath $DestinationFile).Length

        if ($ExistingSize -eq $Item.File.Length) {
            $ExistingCompleteRaw += $Item
            $BatchRaw += $Item
            continue
        }

        Write-Host "Removing incomplete previous copy: $($Item.RelativePath)"
        Remove-Item -LiteralPath $DestinationFile -Force -ErrorAction Stop
    }


    # --------------------------------------------------------
    # Select new files until the safe capacity is reached.
    # --------------------------------------------------------

    if (-not $SpaceLimitReached) {

        [Int64]$NextPlannedBytes = $PlannedNewBytes + [Int64]$Item.File.Length

        if ($NextPlannedBytes -le $AvailableForNewRaw) {
            $NewRawToCopy += $Item
            $BatchRaw += $Item
            $PlannedNewBytes = $NextPlannedBytes
        }
        else {
            $SpaceLimitReached = $true
        }
    }
}


Write-Host ""
Write-Host "RAW selected for this batch : $($BatchRaw.Count)"
Write-Host "Already complete on D       : $($ExistingCompleteRaw.Count)"
Write-Host "New RAW files to copy       : $($NewRawToCopy.Count)"
Write-Host "Planned new RAW size        : $([math]::Round($PlannedNewBytes / 1GB, 2)) GB"
Write-Host "D free before RAW copy      : $(Get-FreeGB) GB"
Write-Host "Reserved free space         : $ReserveFreeGB GB"
Write-Host ""


# ============================================================
# Copy selected RAW files with Robocopy /MT:14.
#
# Files are grouped by source directory. Each group is copied
# in chunks so the native Windows command line does not become
# excessively long.
#
# This keeps Robocopy's multi-file /MT behavior while avoiding:
# - .rcj job files
# - /XF lists
# - Chinese-path encoding problems in job files
# ============================================================

$RoboCode = 0
$RoboFailed = $false


if ($NewRawToCopy.Count -gt 0) {

    New-Item -ItemType Directory -Force -Path $RawTarget | Out-Null

    $DirectoryGroups = @(
        $NewRawToCopy |
            Group-Object { $_.File.DirectoryName }
    )


    foreach ($DirectoryGroup in $DirectoryGroups) {

        if ($RoboFailed) {
            break
        }

        $GroupItems = @($DirectoryGroup.Group)

        if ($GroupItems.Count -eq 0) {
            continue
        }

        $SourceDir = $GroupItems[0].File.DirectoryName

        $FirstRelativeParent = Split-Path -Path $GroupItems[0].RelativePath -Parent

        if ([string]::IsNullOrWhiteSpace($FirstRelativeParent)) {
            $DestinationDir = $RawTarget
        }
        else {
            $DestinationDir = Join-Path $RawTarget $FirstRelativeParent
        }

        New-Item -ItemType Directory -Force -Path $DestinationDir | Out-Null


        $Chunks = @(Split-ItemsIntoChunks -Items $GroupItems -ChunkSize $MaxFilesPerRoboCall)


        foreach ($Chunk in $Chunks) {

            if ($RoboFailed) {
                break
            }

            $Chunk = @($Chunk)

            if ($Chunk.Count -eq 0) {
                continue
            }

            [Int64]$ChunkBytes = 0

            foreach ($Item in $Chunk) {
                $ChunkBytes += [Int64]$Item.File.Length
            }

            Write-Host "------------------------------------------------------------"
            Write-Host "Robocopy batch"
            Write-Host "Source    : $SourceDir"
            Write-Host "Dest      : $DestinationDir"
            Write-Host "Files     : $($Chunk.Count)"
            Write-Host "Size      : $([math]::Round($ChunkBytes / 1GB, 2)) GB"
            Write-Host "Threads   : $RoboThreads"
            Write-Host "D free    : $(Get-FreeGB) GB"
            Write-Host "------------------------------------------------------------"


            $RoboArgs = @(
                $SourceDir
                $DestinationDir
            )

            foreach ($Item in $Chunk) {
                $RoboArgs += $Item.File.Name
            }

            $RoboArgs += @(
                "/MT:$RoboThreads"
                "/J"
                "/DCOPY:DAT"
                "/COPY:DAT"
                "/R:2"
                "/W:2"
                "/NP"
            )


            & robocopy.exe @RoboArgs
            $ThisRoboCode = $LASTEXITCODE

            if ($ThisRoboCode -gt $RoboCode) {
                $RoboCode = $ThisRoboCode
            }

            if ($ThisRoboCode -ge 8) {
                Write-Host ""
                Write-Host "Robocopy reported an error." -ForegroundColor Red
                Write-Host "Exit code: $ThisRoboCode"
                Write-Host "No additional Robocopy chunks will be started."
                Write-Host ""

                $RoboFailed = $true
                break
            }
        }
    }
}


# ============================================================
# Verify every selected RAW independently.
#
# Any incomplete file is removed.
# Every complete file can still be staged even if Robocopy
# reported an error later in the batch.
# ============================================================

$CompleteRaw = @()

foreach ($Item in $BatchRaw) {

    $DestinationFile = Join-Path $RawTarget $Item.RelativePath

    if (Test-Path -LiteralPath $DestinationFile) {

        $DestinationSize = (Get-Item -LiteralPath $DestinationFile).Length

        if ($DestinationSize -eq $Item.File.Length) {
            $CompleteRaw += $Item
        }
        else {
            Write-Host "Removing incomplete RAW: $($Item.RelativePath)" -ForegroundColor Yellow
            Remove-Item -LiteralPath $DestinationFile -Force -ErrorAction Stop
        }
    }
}


$AllRawDone = (
    $CompleteRaw.Count -eq $PendingRaw.Count
)

$IsFinalBatch = $AllRawDone


# If previous batches exist, even the final batch must be tracked
# in archive_files.csv so copy_to_tape.ps1 can mark it ARCHIVED.
$NeedsProgressCsv = (
    $OwnerRows.Count -gt 0
) -or (
    -not $IsFinalBatch
)


# ============================================================
# Create STAGED rows for this batch.
# ============================================================

if ($NeedsProgressCsv) {

    $BatchId = "${Owner}_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    $NewRows = @()


    foreach ($Item in $CompleteRaw) {
        $NewRows += New-ProgressRow `
            -RelativePath $Item.RelativePath `
            -SizeBytes $Item.File.Length `
            -Type "raw"
    }


    if ($TarReady) {
        $TarFile = Get-Item -LiteralPath $TarPath

        $NewRows += New-ProgressRow `
            -RelativePath "__NONRAW_TAR__" `
            -SizeBytes $TarFile.Length `
            -Type "nonraw"
    }


    if ($NewRows.Count -eq 0) {

        Write-Host ""
        Write-Host "No complete new file was staged." -ForegroundColor Red

        if ($RoboFailed) {
            Write-Host "Robocopy failed before a complete new file could be staged."
            Write-Host "Robocopy exit code: $RoboCode"
        }
        elseif ($SpaceLimitReached) {
            Write-Host "The next RAW file cannot fit while keeping the configured reserve."
            Write-Host "Free more D-drive space, then run this script again."
        }
        else {
            Write-Host "There is nothing new to stage."
        }

        Write-Host ""
        pause
        exit 1
    }


    Save-Progress (@($ProgressRows) + @($NewRows))
}


# ============================================================
# Result
# ============================================================

Write-Host ""
Write-Host "============================================================"


if ($IsFinalBatch) {

    Write-Host "STAGING SUCCESS - all remaining RAW files are complete." -ForegroundColor Green

    if ($NeedsProgressCsv) {
        Write-Host "Final batch recorded in: $ProgressPath"
    }
    else {
        Write-Host "Everything fitted in one batch."
        Write-Host "No temporary batch CSV was required."
    }
}
else {

    Write-Host "PARTIAL BATCH READY." -ForegroundColor Yellow
    Write-Host "Complete RAW files staged : $($CompleteRaw.Count)"
    Write-Host "Pending RAW before batch  : $($PendingRaw.Count)"

    if ($TarReady) {
        Write-Host "Non-RAW tar is included in this batch."
    }

    if ($RoboFailed) {
        Write-Host "Reason: Robocopy stopped with exit code $RoboCode."
    }
    elseif ($SpaceLimitReached) {
        Write-Host "Reason: D-drive safe capacity reached."
    }
    else {
        Write-Host "Reason: not all pending RAW files are complete yet."
    }

    Write-Host "Batch recorded in: $ProgressPath"
}


Write-Host "D free now: $(Get-FreeGB) GB"
Write-Host "============================================================"
Write-Host ""
Write-Host "Next: run copy_to_tape.ps1"
Write-Host "Local: $Target"
Write-Host ""

pause
