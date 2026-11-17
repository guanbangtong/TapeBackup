
$SourceRoot = "D:\"
$TapeRoot = "F:\"
$TapeId = "MB2FEQ"
$DeleteAfterSuccessfulCopy = $true

# CSV 或索引写入失败时立即停止，禁止继续删除本地暂存。
$ErrorActionPreference = "Stop"

$IndexDir = Join-Path $SourceRoot "Archive_Index"
$ProgressPath = Join-Path $IndexDir "archive_files.csv"
$ArchiveIndexPath = Join-Path $IndexDir "archive_index.csv"


# ============================================================
# Functions
# ============================================================

function Save-ProgressFile {
    param([object[]]$Rows)

    $Rows = @($Rows)

    if ($Rows.Count -eq 0) {
        if (Test-Path -LiteralPath $ProgressPath) {
            Remove-Item -LiteralPath $ProgressPath -Force
        }
        return
    }

    $TempPath = "$ProgressPath.tmp"
    $Rows |
        Select-Object owner, relative_path, size_bytes, type, batch_id, status, is_final_batch |
        Export-Csv -LiteralPath $TempPath -NoTypeInformation -Encoding UTF8
    Move-Item -LiteralPath $TempPath -Destination $ProgressPath -Force
}


function Add-ArchiveIndexRecord {
    param(
        [string]$Owner,
        [string]$TapeId,
        [Int64]$SizeBytes
    )

    $Record = [PSCustomObject]@{
        date    = Get-Date -Format "yyyy-MM-dd"
        tape_id = $TapeId
        owner   = $Owner
        size_tb = [math]::Round($SizeBytes / 1TB, 3)
    }

    if (Test-Path -LiteralPath $ArchiveIndexPath) {
        $Record | Export-Csv -LiteralPath $ArchiveIndexPath `
            -NoTypeInformation -Encoding UTF8 -Append
    }
    else {
        $Record | Export-Csv -LiteralPath $ArchiveIndexPath `
            -NoTypeInformation -Encoding UTF8
    }

    Write-Host "Archive index updated: $ArchiveIndexPath"
}


New-Item -ItemType Directory -Force -Path $IndexDir | Out-Null

$ArchiveDirectories = @(
    Get-ChildItem -LiteralPath $SourceRoot -Directory -Filter "*_archive"
)


# ============================================================
# 逐个复制暂存目录
# ============================================================

foreach ($Directory in $ArchiveDirectories) {
    $SourceArchive = $Directory.FullName
    $DestinationArchive = Join-Path $TapeRoot $Directory.Name
    $Owner = $Directory.Name.Substring(0, $Directory.Name.Length - "_archive".Length)

    $ProgressRows = @()
    if (Test-Path -LiteralPath $ProgressPath) {
        $ProgressRows = @(Import-Csv -LiteralPath $ProgressPath)
    }

    $OwnerRows = @($ProgressRows | Where-Object { $_.owner -eq $Owner })
    $StagingRows = @($OwnerRows | Where-Object { $_.status -eq "STAGING" })
    $StagedRows = @($OwnerRows | Where-Object { $_.status -eq "STAGED" })

    if ($StagingRows.Count -gt 0) {
        Write-Host ""
        Write-Host "Skipping incomplete staging directory: $SourceArchive"
        Write-Host "Run stage_archive.ps1 again so it can clean and rebuild the batch."
        continue
    }

    # 恢复“磁带成功但本地删除中断”的情况，不再重复复制。
    if ($OwnerRows.Count -gt 0 -and $StagedRows.Count -eq 0) {
        Write-Host ""
        Write-Host "This batch was already verified and marked ARCHIVED."

        $HasFinalRow = @($OwnerRows | Where-Object { $_.is_final_batch -eq "1" }).Count -gt 0

        # 若上次刚好在写正式索引前中断，这里会补写。
        if ($HasFinalRow) {
            [Int64]$RecoveredTotalSizeBytes = 0
            foreach ($Row in $OwnerRows) {
                $RecoveredTotalSizeBytes += [Int64]$Row.size_bytes
            }

            Add-ArchiveIndexRecord `
                -Owner $Owner `
                -TapeId $TapeId `
                -SizeBytes $RecoveredTotalSizeBytes
        }

        if ($DeleteAfterSuccessfulCopy) {
            Write-Host "Cleaning retained local archive: $SourceArchive"
            Remove-Item -LiteralPath $SourceArchive -Recurse -Force
        }

        if ($HasFinalRow) {
            $ProgressRows = @($ProgressRows | Where-Object { $_.owner -ne $Owner })
            Save-ProgressFile $ProgressRows
        }

        continue
    }

    $IsTrackedBatch = ($StagedRows.Count -gt 0)

    if ($IsTrackedBatch) {
        $BatchId = $StagedRows[0].batch_id
    }
    else {
        # 没有 CSV 记录，说明第一个脚本一次就装下了全部文件。
        $BatchId = "single_batch"
    }

    $SourceFiles = @(
        Get-ChildItem -LiteralPath $SourceArchive -Recurse -File -Force
    )

    Write-Host ""
    Write-Host "============================================================"
    Write-Host "Copying to tape"
    Write-Host "Owner : $Owner"
    Write-Host "Batch : $BatchId"
    Write-Host "From  : $SourceArchive"
    Write-Host "To    : $DestinationArchive"
    Write-Host "============================================================"

    New-Item -ItemType Directory -Force -Path $DestinationArchive | Out-Null

    $RoboArgs = @(
        $SourceArchive,
        $DestinationArchive,
        "/E",
        "/DCOPY:DAT",
        "/COPY:DAT",
        "/R:2",
        "/W:5"
    )

    & robocopy.exe @RoboArgs
    $RoboCode = $LASTEXITCODE

    if ($RoboCode -ge 8) {
        Write-Host "Copy failed. robocopy exit code: $RoboCode"
        Write-Host "Local archive was kept: $SourceArchive"
        continue
    }

    # robocopy 返回成功后，再检查本批每个文件的存在性和大小。
    Write-Host "Verifying copied files..."
    $VerificationFailed = $false
    $SourceBase = $SourceArchive.TrimEnd("\")

    foreach ($SourceFile in $SourceFiles) {
        $RelativePath = $SourceFile.FullName.Substring($SourceBase.Length).TrimStart("\")
        $DestinationFile = Join-Path $DestinationArchive $RelativePath

        if (-not (Test-Path -LiteralPath $DestinationFile)) {
            Write-Host "Missing on tape: $RelativePath"
            $VerificationFailed = $true
            break
        }

        $DestinationSize = (Get-Item -LiteralPath $DestinationFile).Length
        if ($DestinationSize -ne $SourceFile.Length) {
            Write-Host "Size mismatch: $RelativePath"
            $VerificationFailed = $true
            break
        }
    }

    if ($VerificationFailed) {
        Write-Host "Tape verification failed. Local archive was kept."
        continue
    }

    Write-Host "Tape copy and verification succeeded."

    $IsFinalBatch = $true
    [Int64]$TotalSizeBytes = 0

    if ($IsTrackedBatch) {
        foreach ($Row in $ProgressRows) {
            if (
                $Row.owner -eq $Owner -and
                $Row.batch_id -eq $BatchId -and
                $Row.status -eq "STAGED"
            ) {
                $Row.status = "ARCHIVED"
            }
        }

        Save-ProgressFile $ProgressRows

        $CurrentBatchRows = @(
            $ProgressRows |
                Where-Object {
                    $_.owner -eq $Owner -and
                    $_.batch_id -eq $BatchId
                }
        )

        $IsFinalBatch = @(
            $CurrentBatchRows |
                Where-Object { $_.is_final_batch -eq "1" }
        ).Count -gt 0

        foreach ($Row in ($ProgressRows | Where-Object { $_.owner -eq $Owner })) {
            $TotalSizeBytes += [Int64]$Row.size_bytes
        }
    }
    else {
        # 单批模式没有文件级 CSV，以本地归档的实际大小写入正式索引。
        foreach ($File in $SourceFiles) {
            $TotalSizeBytes += [Int64]$File.Length
        }
    }

    # 正式索引只在全部批次完成并确认写入磁带后生成。
    if ($IsFinalBatch) {
        Add-ArchiveIndexRecord -Owner $Owner -TapeId $TapeId -SizeBytes $TotalSizeBytes
    }

    $LocalCleanupSucceeded = $true

    if ($DeleteAfterSuccessfulCopy) {
        try {
            Write-Host "Deleting local archive: $SourceArchive"
            Remove-Item -LiteralPath $SourceArchive -Recurse -Force -ErrorAction Stop
        }
        catch {
            $LocalCleanupSucceeded = $false
            Write-Host "Tape is verified, but local deletion failed:"
            Write-Host $_.Exception.Message
        }
    }

    # 最后一批结束后删除该用户的临时文件级记录；其他用户记录不受影响。
    if ($IsTrackedBatch -and $IsFinalBatch -and $LocalCleanupSucceeded) {
        $ProgressRows = @($ProgressRows | Where-Object { $_.owner -ne $Owner })
        Save-ProgressFile $ProgressRows
        Write-Host "Temporary progress records were removed."
    }

    if (-not $IsFinalBatch) {
        Write-Host ""
        Write-Host "This partial batch is complete."
        Write-Host "Run stage_archive.ps1 to stage the next batch."
    }
    else {
        Write-Host ""
        Write-Host "All batches are archived on tape."
    }
}

Write-Host ""
Write-Host "Done."
pause
