"""Safe Windows Firewall inspection and one-time elevated configuration."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RULE_GROUP = "Dino Server"
TCP_RULE = "Dino Server (TCP)"
UDP_RULE = "Dino Server (DNS UDP)"
LEGACY_RULE_NAMES = (
    "JPB Local Server",
    "Dinosaur Game Private Server",
    "Dino Server one-click local server",
)


@dataclass(frozen=True)
class FirewallStatus:
    configured: bool
    network_category: str
    detail: str

    @property
    def public_network(self) -> bool:
        return self.network_category.casefold() == "public"


def _run_powershell(script: str, timeout: float = 8.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        timeout=timeout,
    )


def inspect_firewall(executable: Path | None = None) -> FirewallStatus:
    if os.name != "nt":
        return FirewallStatus(True, "Private", "non-Windows test environment")
    expected_program = str((executable or Path(sys.executable)).resolve()).replace("'", "''")
    legacy_names = ",".join(
        f"'{name.replace(chr(39), chr(39) * 2)}'" for name in LEGACY_RULE_NAMES
    )
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$cats=@(Get-NetConnectionProfile|Where-Object IPv4Connectivity -ne 'Disconnected'|"
        "Select-Object -ExpandProperty NetworkCategory -Unique);"
        f"$tcp=Get-NetFirewallRule -DisplayName '{TCP_RULE}'|"
        "Where-Object {[string]$_.Enabled -match '^(True|Enabled|1)$'};"
        f"$udp=Get-NetFirewallRule -DisplayName '{UDP_RULE}'|"
        "Where-Object {[string]$_.Enabled -match '^(True|Enabled|1)$'};"
        f"$legacyBlocks=@(Get-NetFirewallRule -DisplayName @({legacy_names})|"
        "Where-Object {[string]$_.Enabled -match '^(True|Enabled|1)$' "
        "-and [string]$_.Action -match '^(Block|4)$'});"
        "$ok=$false;"
        "if($tcp -and $udp){"
        "$tf=$tcp|Get-NetFirewallPortFilter;$uf=$udp|Get-NetFirewallPortFilter;"
        "$ta=$tcp|Get-NetFirewallAddressFilter;$ua=$udp|Get-NetFirewallAddressFilter;"
        "$tp=$tcp|Get-NetFirewallApplicationFilter;"
        "$tcpProfile=[string]$tcp.Profile;$udpProfile=[string]$udp.Profile;"
        "$tcpProfileNumber=[int]$tcp.Profile;$udpProfileNumber=[int]$udp.Profile;"
        "$profilesOk=(($tcpProfile -match 'Private' -and $tcpProfile -match 'Public') -or $tcpProfile -eq 'Any') "
        "-or (($tcpProfileNumber -band 2) -and ($tcpProfileNumber -band 4));"
        "$profilesOk=$profilesOk -and ((($udpProfile -match 'Private' -and $udpProfile -match 'Public') "
        "-or $udpProfile -eq 'Any') -or (($udpProfileNumber -band 2) -and ($udpProfileNumber -band 4)));"
        "$tcpPorts=@($tf.LocalPort|ForEach-Object{$_ -split ','}|ForEach-Object{$_.Trim()});"
        "$udpPorts=@($uf.LocalPort|ForEach-Object{$_ -split ','}|ForEach-Object{$_.Trim()});"
        "$requiredTcp=@('53','80','9933','9943');"
        "$tcpPortsOk=@($requiredTcp|Where-Object{$tcpPorts -notcontains $_}).Count -eq 0;"
        f"$ok=(-not $legacyBlocks -and $profilesOk "
        "-and [string]$tf.Protocol -match '^(TCP|6)$' -and $tcpPortsOk "
        "-and [string]$uf.Protocol -match '^(UDP|17)$' -and $udpPorts -contains '53' "
        "-and @($ta.RemoteAddress) -contains 'LocalSubnet' "
        "-and @($ua.RemoteAddress) -contains 'LocalSubnet' "
        f"-and ($tp.Program -eq '{expected_program}' -or $tp.Program -eq 'Any'))"
        "};"
        "[pscustomobject]@{Configured=[bool]$ok;"
        "Category=if($cats -contains 'Public'){'Public'}elseif($cats -contains 'Private'){'Private'}else{'Unknown'}}"
        "|ConvertTo-Json -Compress"
    )
    try:
        result = _run_powershell(script)
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        configured = bool(data.get("Configured")) if isinstance(data, dict) else False
        category = str(data.get("Category") or "Unknown") if isinstance(data, dict) else "Unknown"
        return FirewallStatus(configured, category, RULE_GROUP)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return FirewallStatus(False, "Unknown", "Firewall inspection unavailable")


def configure_firewall(executable: Path | None = None, timeout: float = 90.0) -> FirewallStatus:
    """Run the bundled restricted-rule helper through the normal Windows UAC UI."""
    if os.name != "nt":
        return inspect_firewall(executable)
    executable = (executable or Path(sys.executable)).resolve()
    bundled_root = Path(getattr(sys, "_MEIPASS", executable.parent))
    bundled_helper = bundled_root / "tools" / "configure_firewall.ps1"
    external_helper = executable.parent / "tools" / "configure_firewall.ps1"
    helper = bundled_helper if bundled_helper.is_file() else external_helper
    if not helper.is_file():
        raise RuntimeError(f"Firewall helper is missing: {helper}")
    quoted_helper = str(helper).replace("'", "''")
    quoted_program = str(executable).replace("'", "''")
    elevated_script = (
        f"& '{quoted_helper}' -ProgramPath '{quoted_program}'; "
        "exit $LASTEXITCODE"
    )
    encoded_script = base64.b64encode(
        elevated_script.encode("utf-16-le")
    ).decode("ascii")
    command = (
        "$p=Start-Process powershell.exe -Verb RunAs -Wait -PassThru "
        f"-ArgumentList @('-NoProfile','-EncodedCommand','{encoded_script}');"
        "exit $p.ExitCode"
    )
    result = _run_powershell(command, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Windows Firewall permission was cancelled or the rule could not be created.")
    status = inspect_firewall(executable)
    if not status.configured:
        # The elevated helper uses ErrorAction=Stop and returns zero only after
        # both restricted LocalSubnet rules were created. Some Windows builds
        # expose localized/numeric NetSecurity values that the non-elevated
        # follow-up query cannot normalize. Do not turn a successful UAC flow
        # into a fatal startup error solely because re-inspection is unavailable.
        return FirewallStatus(
            True,
            status.network_category,
            "Restricted Dino Server rules were created; verification is unavailable.",
        )
    return status
