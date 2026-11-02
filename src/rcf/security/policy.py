"""Permission tiers, SSH hardening rules and destructive-command guardrails.

RCF is *not* a sandbox.  What is implemented here is an auditable gate that
keeps an Agent from silently escalating: every entry point maps an operation to
a tier, and the policy decides whether that tier is enabled.  Anything RCF
cannot enforce is documented in ``docs/security.md`` instead of being implied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from ..core.errors import ConfigError, PermissionDenied


class PermissionTier(StrEnum):
    READ_ONLY = "read_only"
    EXECUTE = "execute"
    ENVIRONMENT_MUTATION = "environment_mutation"
    SYSTEM_ADMIN = "system_admin"


#: Every state-changing operation RCF knows about, with the tier it needs.
OPERATION_TIERS: dict[str, PermissionTier] = {
    "node.list": PermissionTier.READ_ONLY,
    "node.inspect": PermissionTier.READ_ONLY,
    "node.probe": PermissionTier.READ_ONLY,
    # Writes and removes one temporary directory under work_root; no job.
    "node.selftest": PermissionTier.EXECUTE,
    "environment.list": PermissionTier.READ_ONLY,
    "environment.inspect": PermissionTier.READ_ONLY,
    "environment.verify": PermissionTier.EXECUTE,
    "environment.plan": PermissionTier.READ_ONLY,
    "project.list": PermissionTier.READ_ONLY,
    "project.inspect": PermissionTier.READ_ONLY,
    "project.sync_plan": PermissionTier.READ_ONLY,
    "project.push_plan": PermissionTier.READ_ONLY,
    "run.plan": PermissionTier.READ_ONLY,
    "run.list": PermissionTier.READ_ONLY,
    "run.status": PermissionTier.READ_ONLY,
    "run.progress": PermissionTier.READ_ONLY,
    "run.wait": PermissionTier.READ_ONLY,
    "run.logs": PermissionTier.READ_ONLY,
    "run.results": PermissionTier.READ_ONLY,
    "storage.scan": PermissionTier.READ_ONLY,
    "storage.report": PermissionTier.READ_ONLY,
    "storage.plan": PermissionTier.READ_ONLY,
    "storage.adopt": PermissionTier.EXECUTE,
    "storage.apply": PermissionTier.EXECUTE,
    "storage.pin": PermissionTier.EXECUTE,
    "run.fetch": PermissionTier.EXECUTE,
    "run.submit": PermissionTier.EXECUTE,
    "run.cancel": PermissionTier.EXECUTE,
    "project.sync": PermissionTier.EXECUTE,
    # Copies files onto a node (creates directories, overwrites files).
    "project.push": PermissionTier.EXECUTE,
    # Gated by path ownership (environments/ownership.py), not by a boolean.
    "environment.apply": PermissionTier.EXECUTE,
    "node.install_agent": PermissionTier.SYSTEM_ADMIN,
    "system.modify": PermissionTier.SYSTEM_ADMIN,
}

#: Commands that RCF refuses to place in a generated remote command line.
#: This is a guardrail against accidents, not a security boundary.
DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bsudo\b", "privilege escalation"),
    (r"(^|\s)su\s+-\s*root", "privilege escalation"),
    (r"\bmkfs(\.\w+)?\b", "filesystem creation"),
    (r"\bdd\s+[^\n]*of=/dev/", "raw device write"),
    (r"\bwipefs\b", "filesystem signature wipe"),
    (r"\bshred\b", "irreversible file destruction"),
    (r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|/\*)\s*($|[;&|])", "recursive delete of /"),
    (r"\brm\s+-rf\s+~/?\s*($|[;&|])", "recursive delete of home"),
    (r"\b(shutdown|reboot|poweroff|halt)\b", "host power state change"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "system service change"),
    (r"\bkill\s+-9\s+1\b", "kill init"),
    (r"\bchmod\s+-R\s+777\s+/", "world-writable root"),
    (r"\bcrontab\s+-r\b", "cron removal"),
    (r"\bautossh\b", "persistent tunnel"),
    (r"ssh\s+[^\n]*-[RD]\s", "reverse/dynamic tunnel"),
    (r"\b(curl|wget)\s+[^\n|]*\|\s*(bash|sh)\b", "pipe-to-shell installer"),
)

_DANGEROUS = tuple((re.compile(pattern, re.IGNORECASE), label) for pattern, label in DANGEROUS_PATTERNS)

#: SSH options that would weaken or disable host key verification.
_FORBIDDEN_SSH_KEYS: dict[str, set[str] | None] = {
    "stricthostkeychecking": {"no", "off", "false", "accept-new", "0"},
    "userknownhostsfile": {"/dev/null", "none", "nul"},
    "globalknownhostsfile": {"/dev/null", "none", "nul"},
    "checkhostip": {"no", "off", "false"},
}
_DENIED_SSH_FLAGS = {"-o", "-F", "-i", "-l", "-p", "-J", "-W", "-L", "-R", "-D"}


@dataclass
class SecurityPolicy:
    allow_execute: bool = True
    allow_environment_mutation: bool = False
    allow_system_admin: bool = False
    require_confirmation: bool = True
    _enabled: dict[PermissionTier, bool] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._enabled = {
            PermissionTier.READ_ONLY: True,
            PermissionTier.EXECUTE: self.allow_execute,
            PermissionTier.ENVIRONMENT_MUTATION: self.allow_environment_mutation,
            PermissionTier.SYSTEM_ADMIN: self.allow_system_admin,
        }

    def tier_of(self, operation: str) -> PermissionTier:
        try:
            return OPERATION_TIERS[operation]
        except KeyError:  # pragma: no cover - programming error
            raise PermissionDenied(
                f"unknown operation {operation!r}; refusing to guess a permission tier",
                detail={"operation": operation},
            ) from None

    def enabled(self, tier: PermissionTier) -> bool:
        return bool(self._enabled.get(tier, False))

    def check(self, operation: str) -> PermissionTier:
        tier = self.tier_of(operation)
        if not self.enabled(tier):
            raise PermissionDenied(
                f"operation {operation!r} requires tier {tier.value!r}, which is disabled in config",
                hint=(
                    "enable it deliberately in rcf.toml under [security] "
                    f"(e.g. allow_{'execute' if tier is PermissionTier.EXECUTE else 'environment_mutation' if tier is PermissionTier.ENVIRONMENT_MUTATION else 'system_admin'} = true)"
                ),
                detail={"operation": operation, "tier": tier.value},
            )
        return tier

    def check_confirmation(self, operation: str, confirmed: bool) -> None:
        if self.require_confirmation and not confirmed:
            raise PermissionDenied(
                f"operation {operation!r} requires explicit confirmation",
                hint="re-run with confirmation (CLI: --yes, MCP: confirm=true) after reviewing the plan",
                detail={"operation": operation, "reason": "confirmation_missing"},
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "allow_execute": self.allow_execute,
            "allow_environment_mutation": self.allow_environment_mutation,
            "allow_system_admin": self.allow_system_admin,
            "require_confirmation": self.require_confirmation,
        }


def normalize_ssh_options(options: list[str]) -> list[str]:
    """Validate user supplied extra ssh options and return them unchanged.

    Only options that cannot weaken host key verification are accepted.  A
    rejection here is a config error: RCF must fail closed rather than silently
    running with an insecure ssh invocation.
    """

    normalized: list[str] = []
    index = 0
    while index < len(options):
        token = options[index].strip()
        if not token:
            index += 1
            continue
        if token == "-o":
            if index + 1 >= len(options):
                raise ConfigError("ssh extra_options: '-o' without a value")
            key_value = options[index + 1].strip()
            index += 2
        elif token.startswith("-o") and len(token) > 2:
            key_value = token[2:].strip()
            index += 1
        elif token.startswith("-"):
            raise ConfigError(
                f"ssh extra_options: flag {token!r} is not allowed; use '-o Key=Value' form",
                hint="RCF manages connection flags itself so its security invariants hold",
                detail={"option": token},
            )
        else:
            raise ConfigError(
                f"ssh extra_options: unexpected token {token!r}",
                detail={"option": token},
            )
        key, _, value = key_value.partition("=")
        key_l = key.strip().lower()
        value_l = value.strip().lower()
        if key_l in _FORBIDDEN_SSH_KEYS:
            blocked = _FORBIDDEN_SSH_KEYS[key_l]
            if blocked is None or value_l in blocked:
                raise ConfigError(
                    f"ssh option {key}={value} would weaken host key verification and is refused",
                    hint="use your ~/.ssh/known_hosts; RCF never disables host key checking",
                    detail={"option": f"{key}={value}"},
                )
        if key_l in {"passwordauthentication", "kbdinteractiveauthentication"} and value_l == "yes":
            raise ConfigError(
                f"ssh option {key}=yes is refused: RCF runs SSH in batch mode and never prompts",
                detail={"option": f"{key}={value}"},
            )
        if key_l == "batchmode" and value_l in {"no", "off", "false"}:
            raise ConfigError("ssh option BatchMode=no is refused: RCF never waits for a prompt")
        normalized.extend(["-o", f"{key}={value}"])
    return normalized


def scan_dangerous_command(text: str) -> list[str]:
    """Return the labels of destructive patterns found in ``text``."""

    hits: list[str] = []
    for regex, label in _DANGEROUS:
        if regex.search(text):
            hits.append(label)
    return hits


def assert_safe_command(text: str, *, context: str) -> None:
    hits = scan_dangerous_command(text)
    if hits:
        raise PermissionDenied(
            f"refused to build {context}: command contains destructive pattern(s) {sorted(set(hits))}",
            hint="system-level changes are out of scope for RCF; run them manually if you really intend to",
            detail={"context": context, "patterns": sorted(set(hits))},
        )
