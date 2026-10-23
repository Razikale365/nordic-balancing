# Security policy

## Supported versions

Security fixes go into the latest minor release only.

## Reporting a vulnerability

Report a vulnerability privately through GitHub's private vulnerability reporting:
use the **Report a vulnerability** button on the repository's Security tab. Do not open a
public issue for a security problem.

## How the library handles credentials

- **Where keys go:** the Fingrid API key is sent in the `x-api-key` request header and the
  ENTSO-E token in the `SECURITY_TOKEN` request header. Neither is ever put in a URL, so
  neither appears in `httpx`'s request log (which logs URLs at `INFO`).
- **Where keys never go:** redirects are never followed, so a key cannot be forwarded to
  another host. A `base_url` for these two clients must use `https` and must not contain a
  user name or password. Error messages redact the key, and drop the provider's text entirely
  if the key could be hidden in it in an encoded form. A response that echoes the key back is
  rejected rather than returned in a record's `raw` data.
- **Your own client:** if you pass your own `httpx.Client`, its event hooks, transports and
  proxies see the request headers, keys included. Treat that code as trusted.
- **Logging boundary:** the library never logs. `httpcore` logs response headers at `DEBUG`,
  and `httpx` logs the response status line at `INFO`; if a server ever echoed your key in a
  response header or status line, it would appear there. None of the supported providers is
  known to do this.

## Upgrading from 0.4.0 or earlier

Up to 0.4.0 the ENTSO-E token was sent in the request URL, and `httpx` logs request URLs at
`INFO`. If your application enabled `INFO` logging (for example
`logging.basicConfig(level=logging.INFO)`) and those logs were kept or shared, rotate your
ENTSO-E token.
