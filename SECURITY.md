# Security policy

This project is a read-only MCP server that reads one person's Octopus Energy
account. The things worth protecting are, in order:

1. **The operator's API key.** It grants full access to their Octopus account,
   including everything this server deliberately refuses to do. It lives in
   `.env`, is sent only to `api.octopus.energy` over HTTPS, and is redacted by
   value from every log line.
2. **The account data itself** — consumption, tariff, meter identifiers and
   address. The MCP endpoint has no caller authentication, so anything that can
   reach it can read all of that.

## Reporting a vulnerability

**Please don't open a public issue for a security problem.**

Use GitHub's private vulnerability reporting: go to the
[Security tab](https://github.com/ngfw-automation/octopus-energy-mcp-server/security)
and click **Report a vulnerability**. That opens a private thread visible only
to you and the maintainer.

Useful things to include: what an attacker can do, the smallest sequence of
steps that shows it, which version or image digest you tested, and whether it
needs any prior access. A proof of concept helps but is not required — a clear
description of the flaw is enough.

**Redact your own identifiers** in the report — MPAN, MPRN, meter serial,
account number, address, API key. A report about a leak shouldn't itself be a
leak.

## What to expect

This is a hobby project with a single maintainer, not a funded product. There
is no paid bounty and no guaranteed response time. In practice: an
acknowledgement within a few days, an assessment of whether it's real and how
bad it is, and a fix on `main` and in the published image when there is one.
Credit in the release notes if you'd like it, and not if you wouldn't.

If a fix is going to take a while and users are exposed in the meantime, that
gets said in the README rather than left quiet.

## In scope

- The API key or any other secret reaching a log line, a tool response, an
  error message, or the image.
- Any way to bypass the Host/Origin validation or otherwise reach the endpoint
  from somewhere the operator hasn't allowed.
- Any way to make the server issue requests it shouldn't (SSRF), or to read
  data belonging to a meter, property or account the caller shouldn't see.
- Cache poisoning across credentials — one account's data served to another.
- A dependency or base-image vulnerability that is actually reachable through
  this code. (Dependabot handles routine version bumps; no need to file those.)
- Anything that turns this read-only server into one that can write to an
  Octopus account.

## Known and accepted

These are documented design limits of the current phase, not findings. Reports
about them are welcome as **issues**, not security reports:

- **The MCP endpoint has no caller authentication.** Loopback binding and
  Host/Origin validation close the network paths to it; they do not
  authenticate a caller. Anything already running on the host — or on the
  tailnet, if Tailscale is used as the README describes — can read the account.
  OAuth 2.1 resource-server support is the intended fix. See *Known limits and
  roadmap* in the README.
- **The API key grants more than the server uses.** Octopus issues one key per
  account with no scoping, so there is no narrower credential to ask for.
- **`.env` is a plaintext file.** It is the operator's own machine; the file is
  gitignored and never enters the image.

## Supported versions

Only the current `main` and the `:latest` image built from it. There are no
maintained release branches, and older SHA-tagged images are not patched.

## For operators

- Treat `.env` as a secret. Rotate the key from Octopus's Developer settings
  page if it has ever been shared, pasted, or committed anywhere — generating a
  new key instantly invalidates the old one.
- Keep the published port on loopback. See *Exposure* in the README before
  changing that.
- Run `docker compose pull` periodically; the images track `:latest` precisely
  so security rebuilds reach you.

---

*This project is not affiliated with, endorsed by, or supported by Octopus
Energy. It is an independent client of their public API.*
