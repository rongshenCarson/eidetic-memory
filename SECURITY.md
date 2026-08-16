# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security vulnerabilities. Report privately instead:

- **Email**: rongshenCarson@users.noreply.github.com (preferred — responds fastest)
- **Alternative**: create a private vulnerability report via GitHub's
  [Security Advisories](https://github.com/rongshenCarson/eidetic-memory/security/advisories/new)

Please include:
- Affected version(s)
- Steps to reproduce
- Impact description

## Response

- **Acknowledgment**: within 48 hours
- **Triage**: within 1 week — we'll confirm scope and severity
- **Fix**: timeline depends on severity; critical issues are prioritized

## Scope

This project is **local-first**: memory data never leaves your machine by design.
Most "breaches" in practice are about what's stored in `raw/` and `data/` — treat those
directories as sensitive. See `docs/config-reference.md` for data locations and redaction
options (`config seal` / `support-bundle` redaction).
