# Security Policy

## Reporting a Vulnerability

Security and privacy are fundamental to this project. If you discover a security vulnerability or privacy leak (such as unexpected network requests or unhandled telemetry), please report it responsibly.

### How to report
- Please **do not** open a public GitHub issue for security vulnerabilities involving potential exposure of sensitive data.
- Send details of the vulnerability along with steps to reproduce to the repository maintainer.
- You can expect an initial response acknowledging your report within 48 hours.

## Security & Privacy Guarantees

This repository is designed with a strict **local-first, privacy-first** guarantee:
1. **No External Network Calls**: During media conversion and text extraction, no network requests are made.
2. **Local AI Inference**: MLX-Whisper and local Ollama instances run exclusively on localhost.
3. **No Telemetry**: No tracking, usage analytics, or remote log collection is present in this codebase.
4. **Data Isolation**: All processed files (`target/`) are kept separate from version-controlled source code.
