"""IMAP verification-code reader, exposed to the agentic apply engine as an MCP
tool (read_verification_code).

When the agent creates an account on an off-site ATS and the site emails a
confirmation link or one-time code, it calls read_verification_code to fetch the
latest one from the candidate's inbox and continue — the gap ApplyPilot's gmail
MCP filled (agent.py notes that gmail MCP was trimmed out of this port).

The pure extraction (extract_verification / find_verification_in) is unit-tested;
the imaplib fetch and the FastMCP stdio server (main) are integration, verified
manually. The `mcp` SDK is imported lazily in main() so this module imports and
unit-tests without it (it's an optional local dep, like playwright/patchright).

By construction this can only ever return a verification-shaped token (a code or
a verify/confirm link) from a RECENT email — never arbitrary inbox content — so
exposing it to the agent doesn't leak the mailbox.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header

from pipeline._batch_common import env_int

# Digits (4-8) count as a code only when a verification keyword sits within ~40
# non-digit chars of them (either order) — so order numbers, prices, years etc.
# don't get mistaken for an OTP.
_KEYWORD = r"(?:code|verif\w*|otp|one[- ]?time|pass ?code|security|2fa|confirm\w*)"
# Boundaries so a 4-8 digit OTP isn't sliced out of a longer number (a 10-digit
# phone/account near a keyword must NOT yield its first 8 digits).
_DIGITS = r"(?<!\d)(\d{4,8})(?!\d)"
_CODE_PATTERNS = (
    re.compile(rf"{_KEYWORD}[^\d]{{0,40}}{_DIGITS}", re.I),   # "your code is 482913"
    re.compile(rf"{_DIGITS}[^\d]{{0,40}}{_KEYWORD}", re.I),   # "482913 is your code"
)
# A confirmation/verification link — its path or query names the action.
_LINK = re.compile(r"https?://\S*(?:verif|confirm|activat|validat)\S*", re.I)


def extract_verification(text: str) -> str | None:
    """A verification code (OTP digits near a verification keyword) or a
    confirm/verify link from one email's text. A typeable code wins when both are
    present. None if neither is found."""
    text = text or ""
    for pat in _CODE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    m = _LINK.search(text)
    if m:
        return m.group(0).rstrip(".,);:\"'>]")   # trim trailing prose punctuation
    return None


def find_verification_in(emails: list[dict]) -> str | None:
    """The verification token from the most recent email (emails newest-first)
    that has one. None if none do."""
    for em in emails:
        token = extract_verification(f"{em.get('subject', '')}\n{em.get('body', '')}")
        if token:
            return token
    return None


# ── IMAP fetch (integration — verified manually) ─────────────────────────────

def _decode_subject(raw: str) -> str:
    out = []
    for part, enc in decode_header(raw or ""):
        out.append(part.decode(enc or "utf-8", "replace") if isinstance(part, bytes) else part)
    return "".join(out)


def _email_text(msg) -> str:
    """The text/plain body (falling back to any text/* part)."""
    def decode(part) -> str:
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        return payload.decode(part.get_content_charset() or "utf-8", "replace")

    if not msg.is_multipart():
        return decode(msg)
    plain = [decode(p) for p in msg.walk() if p.get_content_type() == "text/plain"]
    if any(plain):
        return "\n".join(plain)
    for p in msg.walk():
        if p.get_content_type().startswith("text/"):
            return decode(p)
    return ""


def _recent_emails(*, host: str, port: int, user: str, password: str,
                   limit: int = 10) -> list[dict]:
    """The most recent `limit` inbox messages (newest first). SINCE-yesterday
    keeps the fetch small and bounds how far back a stale code could come from."""
    conn = imaplib.IMAP4_SSL(host, port)
    try:
        conn.login(user, password)
        conn.select("INBOX")
        since = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
        _, data = conn.search(None, "SINCE", since)
        ids = data[0].split()[-limit:]
        out = []
        for eid in reversed(ids):   # newest first
            _, msg_data = conn.fetch(eid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            out.append({"subject": _decode_subject(msg.get("Subject", "")),
                        "body": _email_text(msg)})
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def read_verification_code() -> str:
    """MCP tool: the latest email verification code/link from the inbox configured
    via APPLY_ATS_EMAIL + APPLY_IMAP_*. Returns a short status string the agent can
    act on rather than raising (so a transient inbox issue doesn't kill the run)."""
    host = os.environ.get("APPLY_IMAP_HOST", "").strip()
    user = os.environ.get("APPLY_ATS_EMAIL", "").strip()
    password = os.environ.get("APPLY_IMAP_PASSWORD", "").strip()
    port = env_int("APPLY_IMAP_PORT", 993)
    if not (host and user and password):
        return "NOT_CONFIGURED"
    try:
        emails = _recent_emails(host=host, port=port, user=user, password=password)
    except Exception as e:
        return f"ERROR: {type(e).__name__}"
    return find_verification_in(emails) or "NO_CODE_FOUND"


def main() -> None:
    """Run the stdio MCP server exposing read_verification_code. The `mcp` SDK is
    imported lazily so the rest of the module needs no extra dependency."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover - exercised only without the dep
        raise ImportError(
            "The IMAP verification tool needs the `mcp` SDK. Install it locally:\n"
            "  pip install mcp"
        ) from e
    server = FastMCP("imap-verification")
    server.tool()(read_verification_code)
    server.run()


if __name__ == "__main__":
    main()
