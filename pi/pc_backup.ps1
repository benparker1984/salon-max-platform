param(
    [string]$BackupRoot = (Join-Path $env:USERPROFILE 'SalonMaxBackups'),
    [string]$PiHost = 'benparker1984@100.71.173.23',
    [switch]$VerifyLatest,
    [string]$ExtractLatestTo = ''
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$backupDirectory = Join-Path $BackupRoot 'backups'
$keyPath = Join-Path $BackupRoot 'backup.key'
$statusPath = Join-Path $BackupRoot 'status.json'
[IO.Directory]::CreateDirectory($backupDirectory) | Out-Null
$lock = $null

function Get-Sha256([byte[]]$Bytes) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
}

function Protect-Archive([byte[]]$Bytes, [byte[]]$Key) {
    $aes = [Security.Cryptography.Aes]::Create()
    try {
        $aes.Key = [byte[]]$Key[0..31]
        $aes.GenerateIV()
        $cipher = $aes.CreateEncryptor().TransformFinalBlock($Bytes, 0, $Bytes.Length)
        [byte[]]$payload = [Text.Encoding]::ASCII.GetBytes('SMBACKUP1') + $aes.IV + $cipher
        $hmac = [Security.Cryptography.HMACSHA256]::new([byte[]]$Key[32..63])
        try { return ,([byte[]]($payload + $hmac.ComputeHash($payload))) }
        finally { $hmac.Dispose() }
    } finally { $aes.Dispose() }
}

function Unprotect-Archive([byte[]]$Bytes, [byte[]]$Key) {
    if ($Bytes.Length -lt 73 -or [Text.Encoding]::ASCII.GetString($Bytes, 0, 9) -ne 'SMBACKUP1') {
        throw 'Unrecognised backup format'
    }
    $length = $Bytes.Length - 32
    $hmac = [Security.Cryptography.HMACSHA256]::new([byte[]]$Key[32..63])
    try { $tag = $hmac.ComputeHash($Bytes, 0, $length) } finally { $hmac.Dispose() }
    $difference = 0
    for ($i=0; $i -lt 32; $i++) { $difference = $difference -bor ($tag[$i] -bxor $Bytes[$length+$i]) }
    if ($difference -ne 0) { throw 'Backup authentication failed: wrong key or damaged file' }
    $aes = [Security.Cryptography.Aes]::Create()
    try {
        $aes.Key = [byte[]]$Key[0..31]
        $aes.IV = [byte[]]$Bytes[9..24]
        return ,($aes.CreateDecryptor().TransformFinalBlock($Bytes, 25, $length-25))
    } finally { $aes.Dispose() }
}

function Test-Bundle([byte[]]$Bytes, [string]$Destination = '') {
    $stream = [IO.MemoryStream]::new($Bytes, $false)
    $zip = [IO.Compression.ZipArchive]::new($stream, [IO.Compression.ZipArchiveMode]::Read)
    try {
        $entry = $zip.GetEntry('manifest.json')
        if (-not $entry) { throw 'Missing backup manifest' }
        $reader = [IO.StreamReader]::new($entry.Open())
        try { $manifest = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
        if ($manifest.format -ne 'salonmax-recovery-bundle' -or $manifest.version -ne 1) { throw 'Invalid recovery bundle' }
        if ($manifest.databases.'databases/till_backoffice.db'.integrity -ne 'ok') { throw 'Live database integrity was not verified' }
        if ($Destination) {
            $destinationPath = [IO.Path]::GetFullPath($Destination)
            if (Test-Path -LiteralPath $destinationPath) { throw 'Extraction destination must not already exist' }
        }
        foreach ($property in $manifest.files.PSObject.Properties) {
            $name = $property.Name
            if ($name.StartsWith('/') -or $name.Contains('\') -or $name.Split('/') -contains '..') { throw 'Unsafe archive path' }
            $entry = $zip.GetEntry($name)
            if (-not $entry -or $entry.Length -ne $property.Value.bytes) { throw 'Backup file missing or wrong size' }
            $fileStream = $entry.Open()
            $buffer = [IO.MemoryStream]::new()
            try { $fileStream.CopyTo($buffer); $data = $buffer.ToArray() }
            finally { $fileStream.Dispose(); $buffer.Dispose() }
            if ((Get-Sha256 $data) -ne $property.Value.sha256) { throw 'Backup file checksum failed' }
        }
        if ($Destination) {
            [IO.Directory]::CreateDirectory($destinationPath) | Out-Null
            foreach ($entry in $zip.Entries) {
                if ($entry.FullName -ne 'manifest.json' -and -not $manifest.files.PSObject.Properties[$entry.FullName]) { throw 'Unlisted archive entry' }
                $target = [IO.Path]::GetFullPath((Join-Path $destinationPath $entry.FullName))
                if (-not $target.StartsWith($destinationPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe extraction path' }
                [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $false)
            }
        }
        return $manifest
    } finally { $zip.Dispose(); $stream.Dispose() }
}

try {
    $lock = [IO.File]::Open((Join-Path $BackupRoot 'backup.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    if (-not (Test-Path -LiteralPath $keyPath)) {
        if ($VerifyLatest -or $ExtractLatestTo -or (Get-ChildItem $backupDirectory -Filter '*.smbak')) {
            throw 'Recovery encryption key is missing; existing backups cannot be opened without it'
        }
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        $newKey = New-Object byte[] 64
        try { $random.GetBytes($newKey) } finally { $random.Dispose() }
        [IO.File]::WriteAllBytes($keyPath, $newKey)
    }
    [byte[]]$key = [IO.File]::ReadAllBytes($keyPath)
    if ($key.Length -ne 64) { throw 'Invalid recovery key' }
    if ($VerifyLatest -or $ExtractLatestTo) {
        $latest = Get-ChildItem $backupDirectory -Filter '*.smbak' | Sort-Object Name -Descending | Select-Object -First 1
        if (-not $latest) { throw 'No saved backup exists' }
        $manifest = Test-Bundle (Unprotect-Archive ([IO.File]::ReadAllBytes($latest.FullName)) $key) $ExtractLatestTo
        Write-Output ('Backup verified: ' + $latest.Name + '; hosted database: ' + $manifest.cloud_status)
        exit 0
    }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
    $start.Arguments = '-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 ' + $PiHost + ' python3 /home/benparker1984/Salon-Max/ops/create_backup_bundle.py'
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    $memory = [IO.MemoryStream]::new()
    try {
        $process.Start() | Out-Null
        $copy = $process.StandardOutput.BaseStream.CopyToAsync($memory)
        $errors = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(240000)) { $process.Kill(); throw 'Backup SSH connection timed out' }
        $copy.GetAwaiter().GetResult()
        $errors.GetAwaiter().GetResult() | Out-Null
        if ($process.ExitCode -ne 0 -and $process.ExitCode -ne 2) { throw ('Pi backup command failed: exit ' + $process.ExitCode) }
        [byte[]]$plain = $memory.ToArray()
    } finally { $memory.Dispose(); $process.Dispose() }
    $manifest = Test-Bundle $plain
    $name = 'salonmax-' + [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH-mm-ssZ') + '.smbak'
    $file = Join-Path $backupDirectory $name
    $temporary = $file + '.partial'
    [IO.File]::WriteAllBytes($temporary, (Protect-Archive $plain $key))
    # Verify the actual saved encrypted bytes before publishing a successful run.
    Test-Bundle (Unprotect-Archive ([IO.File]::ReadAllBytes($temporary)) $key) | Out-Null
    [IO.File]::Move($temporary, $file)
    $complete = $manifest.cloud_status -eq 'ok'
    @{state=$(if ($complete) {'ok'} else {'partial'}); checked_at=[DateTime]::UtcNow.ToString('o');
      last_file=$name; cloud_status=$manifest.cloud_status;
      customers=$manifest.databases.'databases/till_backoffice.db'.customers;
      total_minutes=$manifest.databases.'databases/till_backoffice.db'.total_minutes;
      message=$(if ($complete) {'Pi and hosted database captured and verified'} else {'Pi saved; hosted database unavailable'})
    } | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
    # Daily retention: keep 30 days, plus every copy made in the last 24 hours.
    # Never prune on a partial or failed run.
    if ($complete) {
        $groups = Get-ChildItem $backupDirectory -Filter '*.smbak' | Sort-Object Name -Descending | Group-Object {$_.Name.Substring(9,10)}
        $expired = $groups | Select-Object -Skip 30 | ForEach-Object {$_.Group}
        foreach ($old in $expired) {
            $resolved = [IO.Path]::GetFullPath($old.FullName)
            if ([IO.Path]::GetDirectoryName($resolved) -ne [IO.Path]::GetFullPath($backupDirectory)) { throw 'Retention path escaped backup directory' }
            if ($old.LastWriteTimeUtc -lt [DateTime]::UtcNow.AddDays(-1)) { Remove-Item -LiteralPath $resolved }
        }
    }
    Write-Output ('Saved and verified ' + $name + '; hosted database: ' + $manifest.cloud_status)
    if (-not $complete) { exit 2 }
} catch {
    @{state='error';checked_at=[DateTime]::UtcNow.ToString('o');message=$_.Exception.Message} |
        ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
    Write-Error $_
    exit 1
} finally {
    if ($lock) { $lock.Dispose() }
}
