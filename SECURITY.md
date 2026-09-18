# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in AniFlow, please report it responsibly:

1. **Do NOT** open a public GitHub issue for security vulnerabilities
2. Email the maintainer directly or use [GitHub's private vulnerability reporting](https://github.com/aashish254/Aniflow/security/advisories/new)
3. Include:
   - A description of the vulnerability
   - Steps to reproduce the issue
   - Potential impact
   - Suggested fix (if any)

## Response Timeline

- **Acknowledgment**: Within 48 hours
- **Assessment**: Within 1 week
- **Fix**: Depending on severity, typically within 2 weeks

## Security Best Practices for Users

- Never commit `.env` files or API keys
- Keep dependencies updated (`pip install --upgrade -r requirements.txt`)
- Run the application in a virtual environment
- Do not expose the Flask development server to the public internet
