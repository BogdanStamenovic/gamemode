# Static checks for the Windows task scripts, runnable with any pwsh (Linux too):
#   pwsh -NoProfile -File scripts/check-ps.ps1 FILE...
# 1. syntax errors (the PowerShell parser);
# 2. functions whose names shadow built-in aliases. Both real bugs so far were
#    this: a helper named H (Get-History) and one named Mp (Move-ItemProperty).
#    PowerShell resolves aliases before functions, so the helper never runs.
$bad = 0
foreach ($f in $args) {
  $tokens = $null; $errors = $null
  $ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f), [ref]$tokens, [ref]$errors)
  foreach ($e in $errors) { $bad++; "{0}:{1}: syntax: {2}" -f $f, $e.Extent.StartLineNumber, $e.Message }
  $fns = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
  foreach ($fn in $fns) {
    if (Get-Alias -Name $fn.Name -ErrorAction SilentlyContinue) {
      $bad++; "{0}:{1}: function '{2}' is shadowed by a built-in alias" -f $f, $fn.Extent.StartLineNumber, $fn.Name
    }
  }
  if (-not $errors.Count) { "parsed $f" }
}
exit $bad
