# Security policy

## Supported versions

Security fixes go into the latest minor release only.

## Reporting a vulnerability

Report a vulnerability privately through GitHub's private vulnerability reporting:
use the **Report a vulnerability** button on the repository's Security tab. Do not open a
public issue for a security problem.

## How the library handles credentials

- **Fingrid:** the API key is sent in the `x-api-key` request header. The client never follows a
  redirect on a Fingrid request, so the key cannot be forwarded to another host.
- **ENTSO-E:** the token is sent as a query parameter, as the API requires.
- **Logging:** if you enable INFO logging for the `httpx` logger, request URLs are logged, and
  with them the ENTSO-E token.
