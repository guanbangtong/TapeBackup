  $NasRoot = "Z:\"
  $RecycleRoot = "Z:\#recycle"

  $Person = Read-Host "Input person folder name to DELETE"

  if ([string]::IsNullOrWhiteSpace($Person)) {
      Write-Host "Empty name. Stop."
      pause
      exit 1
  }

  if ($Person -match '[\\/:*?"<>|]') {
      Write-Host "Invalid name: $Person"
      pause
      exit 1
  }

  $PersonPath = Join-Path $NasRoot $Person
  $RecyclePersonPath = Join-Path $RecycleRoot $Person

  Write-Host ""
  Write-Host "Will delete:"
  Write-Host "  $PersonPath"
  Write-Host "  $RecyclePersonPath"
  Write-Host ""

  $ConfirmName = Read-Host "Type the exact person name to confirm"
  if ($ConfirmName -ne $Person) {
      Write-Host "Name mismatch. Stop."
      pause
      exit 1
  }

  $ConfirmDelete = Read-Host "Type DELETE to permanently delete"
  if ($ConfirmDelete -ne "DELETE") {
      Write-Host "Not confirmed. Stop."
      pause
      exit 1
  }

  $EmptyDir = Join-Path $env:TEMP "empty_delete_dir"
  New-Item -ItemType Directory -Force -Path $EmptyDir | Out-Null

  function Clear-And-RemoveFolder {
      param(
          [string]$Path
      )

      if (!(Test-Path -LiteralPath $Path)) {
          Write-Host "Skip, not found: $Path"
          return
      }

      Write-Host ""
      Write-Host "Clearing: $Path"

      robocopy $EmptyDir $Path /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP

      Write-Host "Removing empty folder: $Path"
      Remove-Item -LiteralPath $Path -Recurse -Force
  }

  Clear-And-RemoveFolder $PersonPath
  Clear-And-RemoveFolder $RecyclePersonPath

  Remove-Item -LiteralPath $EmptyDir -Recurse -Force

  Write-Host ""
  Write-Host "Done."
  pause