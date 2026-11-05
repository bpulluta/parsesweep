# Security

ParseSweep processes untrusted documents and model output. Do not equate
successful extraction or a passing test suite with a security certification.

See the [threat model, deployment controls, migration notes, and HPC acceptance
checklist](docs/security.md) before processing sensitive data.

Report suspected vulnerabilities privately through this repository's GitHub
private vulnerability reporting facility, if enabled, or an established private
maintainer channel. Do not publish credentials, documents, exploitation
payloads containing real data, or institution-specific network details in an
issue. Include affected versions, synthetic reproduction steps, and impact.

Only the current maintained release is targeted for security fixes. Python
3.12 is the execution-tested baseline. Locked dependencies are audited in CI;
an unresolved advisory blocks production acceptance unless the responsible
security authority records an explicit, evidence-backed exception.
