[CmdletBinding()]
param(
    [ValidateSet('Install','Uninstall')]
    [string]$Mode = 'Install',
    [string]$GamePath
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-Sha([string]$Path) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
        } finally { $stream.Dispose() }
    } finally { $hash.Dispose() }
}

function Assert-GameClosed([string]$Exe) {
    foreach ($process in @(Get-Process -Name AThousandCloys -ErrorAction SilentlyContinue)) {
        if (!$process.Path -or $process.Path -eq $Exe) {
            throw 'Закройте игру и запустите установщик снова.'
        }
    }
}

function Resolve-GameFile([string]$Root, [string]$Relative) {
    $path = $Root
    foreach ($part in $Relative.Split('/')) {
        if (!$part -or $part -eq '.' -or $part -eq '..' -or $part -match '[:\\]') {
            throw "Неверный путь в манифесте: $Relative"
        }
        $path = Join-Path $path $part
        $item = Get-Item -LiteralPath $path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "В пути к файлу игры обнаружена ссылка: $path"
        }
    }
    $path
}

function Write-PatchedFile([string]$Source, [string]$Delta, [string]$Destination, [long]$Size) {
    $base = [IO.File]::ReadAllBytes($Source)
    $reader = [IO.BinaryReader]::new([IO.File]::OpenRead($Delta))
    try {
        $out = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew)
        try {
            if ([Text.Encoding]::ASCII.GetString($reader.ReadBytes(6)) -cne 'ATCRU1') {
                throw "Неверный заголовок патча: $Delta"
            }
            while ($reader.BaseStream.Position -lt $reader.BaseStream.Length) {
                $kind = $reader.ReadByte()
                switch ($kind) {
                    0 {
                        $at = [long]$reader.ReadUInt32()
                        $length = [long]$reader.ReadUInt32()
                        if (!$length -or $at + $length -gt $base.LongLength -or $out.Position + $length -gt $Size) {
                            throw 'Патч ссылается на несуществующий диапазон файла.'
                        }
                        $out.Write($base, [int]$at, [int]$length)
                    }
                    1 {
                        $length = [long]$reader.ReadUInt32()
                        if (!$length -or $length -gt $reader.BaseStream.Length - $reader.BaseStream.Position -or $out.Position + $length -gt $Size) {
                            throw 'Патч содержит неполные данные.'
                        }
                        $chunk = $reader.ReadBytes([int]$length)
                        if ($chunk.Length -ne $length) { throw 'Патч повреждён.' }
                        $out.Write($chunk, 0, $chunk.Length)
                    }
                    default { throw 'В патче обнаружена неизвестная команда.' }
                }
            }
            if ($out.Position -ne $Size) { throw 'Размер полученного файла не совпал с манифестом.' }
        } finally { $out.Dispose() }
    } finally { $reader.Dispose() }
}

try {
    if (!$GamePath) {
        Write-Host 'Откройте папку игры в Steam: Библиотека → A Thousand Cloys → Управление → Просмотреть локальные файлы.'
        $GamePath = Read-Host 'Вставьте путь к папке с AThousandCloys.exe'
    }
    if ([string]::IsNullOrWhiteSpace($GamePath)) { throw 'Путь к игре не указан.' }
    $root = (Get-Item -LiteralPath $GamePath.Trim().Trim('"')).FullName
    $exe = Join-Path $root 'AThousandCloys.exe'
    if (!(Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'В выбранной папке нет AThousandCloys.exe.' }
    Assert-GameClosed $exe

    $lockPath = Join-Path $root '.atc-ru.lock'
    if (Test-Path -LiteralPath $lockPath) {
        if ((Get-Item -LiteralPath $lockPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Файл блокировки является ссылкой.'
        }
    }
    $lock = [IO.FileStream]::new($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None, 4096, [IO.FileOptions]::DeleteOnClose)
    try {
        $manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'manifest.json') -Raw | ConvertFrom-Json
        $backup = Join-Path $root '.atc-ru-backup'
        $backupExists = Test-Path -LiteralPath $backup
        if ($backupExists) {
            $item = Get-Item -LiteralPath $backup -Force
            if (!$item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Некорректная папка резервной копии .atc-ru-backup.'
            }
        }
        $files = foreach ($entry in $manifest.files) {
            if ($entry.id -notmatch '^[0-9]{2}$') { throw 'Неверный идентификатор патча.' }
            $fullPath = Resolve-GameFile $root $entry.path
            $current = Get-Sha $fullPath
            if ($current -cne $entry.original -and $current -cne $entry.modified -and $current -cnotin @($entry.previous)) {
                throw "Файл другой версии или уже изменён: $($entry.path). Для мода нужна сборка Steam $($manifest.steamBuild). Можно восстановить файлы через Steam."
            }
            [pscustomobject]@{ Entry = $entry; Target = $fullPath; Current = $current }
        }
        $changed = @($files | Where-Object { $_.Current -cne $_.Entry.original })
        if ($Mode -eq 'Uninstall' -and !$changed.Count) {
            Write-Host 'Файлы уже в исходном состоянии.'
            exit 0
        }
        if (!$backupExists) {
            if ($changed.Count) {
                throw 'Отсутствует резервная копия .atc-ru-backup. Для восстановления проверьте целостность файлов через Steam.'
            }
            $draft = Join-Path $root ('.atc-ru-backup-' + [guid]::NewGuid().ToString('N'))
            [void][IO.Directory]::CreateDirectory($draft)
            foreach ($file in $files) {
                $copy = Join-Path $draft ($file.Entry.id + '.original')
                [IO.File]::Copy($file.Target, $copy)
                if ((Get-Sha $copy) -cne $file.Entry.original) {
                    throw "Не удалось сверить резервную копию: $($file.Entry.path)"
                }
            }
            [IO.Directory]::Move($draft, $backup)
        }

        $id = [guid]::NewGuid().ToString('N')
        $pending = @()
        try {
            foreach ($file in $files) {
                $entry = $file.Entry
                $original = Resolve-GameFile $backup ($entry.id + '.original')
                if ((Get-Sha $original) -cne $entry.original) {
                    throw "Повреждена резервная копия файла $($entry.path). Восстановите файлы через Steam."
                }
                $wanted = if ($Mode -eq 'Install') { $entry.modified } else { $entry.original }
                if ($file.Current -ceq $wanted) { continue }

                $work = Join-Path $backup "$id-$($entry.id).pending"
                $previous = Join-Path $backup "$id-$($entry.id).previous"
                $job = [pscustomobject]@{ File = $file; Work = $work; Previous = $previous; Expected = $wanted; Applied = $false }
                $pending += $job
                if ($Mode -eq 'Install') {
                    $delta = Join-Path $PSScriptRoot "patches/$($entry.id).atcd"
                    Write-PatchedFile $original $delta $work $entry.size
                } else {
                    [IO.File]::Copy($original, $work)
                }
                if ((Get-Sha $work) -cne $wanted) { throw "Проверка результата не прошла: $($entry.path)" }
            }
            Assert-GameClosed $exe
            try {
                foreach ($job in $pending) {
                    if ((Get-Sha $job.File.Target) -cne $job.File.Current) {
                        throw "Файл изменился во время установки: $($job.File.Entry.path)"
                    }
                    [IO.File]::Replace($job.Work, $job.File.Target, $job.Previous)
                    $job.Applied = $true
                }
                foreach ($job in $pending) {
                    if ((Get-Sha $job.File.Target) -cne $job.Expected) { throw 'Проверка установленных файлов не прошла.' }
                }
            } catch {
                for ($i = $pending.Count - 1; $i -ge 0; $i--) {
                    $job = $pending[$i]
                    if ($job.Applied -and (Get-Sha $job.File.Target) -ceq $job.Expected) {
                        $undone = $job.Previous + '.undone'
                        [IO.File]::Replace($job.Previous, $job.File.Target, $undone)
                        [IO.File]::Delete($undone)
                    }
                }
                throw
            }
            foreach ($job in $pending) { [IO.File]::Delete($job.Previous) }
        } finally {
            foreach ($job in $pending) { [IO.File]::Delete($job.Work) }
        }
        if ($Mode -eq 'Install') {
            Write-Host 'Перевод установлен. Запускайте A Thousand Cloys из библиотеки Steam. При необходимости выберите «Русский» в главном меню.'
        } else {
            Write-Host 'Исходные файлы восстановлены. Steam-сохранения находятся в штатном профиле.'
        }
    } finally { $lock.Dispose() }
} catch {
    Write-Host "Ошибка: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
