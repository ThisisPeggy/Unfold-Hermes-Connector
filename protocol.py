"""Small, dependency-free helpers for the Browser connector protocol."""

import hmac


TOKEN_PROTOCOL_PREFIX = "hermes-browser-token."


def token_subprotocol(token):
    """Return the WebSocket subprotocol used to carry a loopback pairing token."""
    clean = str(token or "").strip()
    if len(clean) < 32 or any(ch.isspace() for ch in clean):
        raise ValueError("Invalid Unfold pairing token.")
    return f"{TOKEN_PROTOCOL_PREFIX}{clean}"


def authenticated_subprotocol(header, expected_token):
    """Return the matching offered protocol without putting secrets in the URL."""
    expected = str(expected_token or "").strip()
    if not expected:
        return ""
    for offered in str(header or "").split(","):
        protocol = offered.strip()
        if not protocol.startswith(TOKEN_PROTOCOL_PREFIX):
            continue
        supplied = protocol[len(TOKEN_PROTOCOL_PREFIX):]
        if hmac.compare_digest(supplied, expected):
            return protocol
    return ""
