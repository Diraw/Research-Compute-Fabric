# Security Policy

RCF is a pre-1.0 research tool and **is not a sandbox**. It runs jobs with the
current account's permissions; its checks are guardrails against mistakes, not
isolation from hostile code. The implemented guarantees and the explicit
non-goals are listed in [docs/security.md](docs/security.md).

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x (current pre-release) | Latest commit / release only |
| older snapshots | No |

## In scope

- Bypassing the plan → confirm → submit gate, the storage guards, or the
  path-ownership checks for environment writes.
- Weakening SSH host-key verification, or leaking credentials through config,
  logs or audit records.
- Forging or silently invalidating copy receipts, reference fingerprints or
  deletion journals.
- Writing a run's artifacts over another run's recorded outputs.

## Out of scope

Multi-user isolation, protection from a malicious job or script, remote file
tampering, global GPU scheduling, and any issue that requires the attacker to
already control the account or the machine. These are documented limitations,
not vulnerabilities.

## Reporting

Do not open a public issue with exploit details. Use the code host's private
security advisory feature (for GitHub: *Security → Report a vulnerability*), or
contact the maintainer through the private channel used to obtain this
checkout. Include:

- the affected version or commit,
- a minimal reproduction with the exact commands and configuration (redact
  hostnames, aliases and paths),
- the observed result and the result you expected,
- whether any real node, job or stored artifact was affected.

You will get an acknowledgement and an assessment of whether the report is in
scope. Because this is a research project, no response-time SLA is offered.
