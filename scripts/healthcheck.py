"""Container healthcheck: the MCP server is up if it answers HTTP at all.

A GET to /mcp without a session id returns an HTTP error (400/405/406) —
that is a healthy server. No TCP connection means it is down.
"""

import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8000/mcp", timeout=3)
except urllib.error.HTTPError:
    sys.exit(0)  # any HTTP response means the server is up
except Exception:
    sys.exit(1)
