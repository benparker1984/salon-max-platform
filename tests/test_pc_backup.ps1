$ErrorActionPreference = 'Stop'
$path = Join-Path $PSScriptRoot '../pi/pc_backup.ps1'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Resolve-Path $path), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Backup script has PowerShell syntax errors' }
$functions = $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $false)
foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }
$key = New-Object byte[] 64
$random = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $random.GetBytes($key) } finally { $random.Dispose() }
$plain = [Text.Encoding]::UTF8.GetBytes('Customer account and minutes recovery test')
$encrypted = Protect-Archive $plain $key
$restored = Unprotect-Archive $encrypted $key
if ([Convert]::ToBase64String($plain) -ne [Convert]::ToBase64String($restored)) { throw 'Encryption round-trip failed' }
$second = Protect-Archive $plain $key
if ([Convert]::ToBase64String($encrypted) -eq [Convert]::ToBase64String($second)) { throw 'IV was reused' }
$encrypted[30] = $encrypted[30] -bxor 1
$rejected = $false
try { Unprotect-Archive $encrypted $key | Out-Null } catch { $rejected = $true }
if (-not $rejected) { throw 'Tampered backup was accepted' }
$wrongKey = New-Object byte[] 64
$rejected = $false
try { Unprotect-Archive $second $wrongKey | Out-Null } catch { $rejected = $true }
if (-not $rejected) { throw 'Wrong key was accepted' }
Write-Output 'Passed: PowerShell parsing, encryption round-trip, fresh IV, tamper rejection, wrong-key rejection.'
