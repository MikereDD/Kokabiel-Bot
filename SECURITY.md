# Security Policy

## Secrets and private configuration

Never commit a live Telegram bot token, TMDB bearer token, private runtime
configuration, database, or log file to this repository.

Kokabiel is designed to keep its real configuration outside the repository at
`$HOME/bots/config/kokabielrc.py` by default. The checked-in
`kokabielrc.example.py` contains placeholders only.

If a credential is ever committed, treat it as compromised: revoke or rotate
it first, then remove it from Git history as a separate cleanup step.

## Reporting a vulnerability

Please do not publish credentials, private chat data, or an exploitable security
issue in a public issue. Contact the project owner privately or use GitHub's
private vulnerability-reporting/security-advisory feature when it is enabled
for the repository.
