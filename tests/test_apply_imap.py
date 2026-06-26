"""Unit tests for the IMAP verification reader's pure logic (extraction +
selection). The imaplib fetch and the FastMCP stdio server are integration,
verified manually — these cover only what's deterministic and parseable."""

from pipeline.apply import imap_mcp


class TestExtractVerification:
    def test_otp_near_keyword(self):
        assert imap_mcp.extract_verification("Your verification code is 482913.") == "482913"

    def test_otp_with_colon(self):
        assert imap_mcp.extract_verification("Security code: 246810") == "246810"

    def test_otp_keyword_after_digits(self):
        # Digits before the keyword — the inverse-order pattern.
        assert imap_mcp.extract_verification("482913 is your verification code") == "482913"

    def test_confirmation_link(self):
        body = "Confirm your account: https://ats.example.com/verify?token=abc123 — thanks!"
        assert imap_mcp.extract_verification(body) == "https://ats.example.com/verify?token=abc123"

    def test_code_preferred_over_link(self):
        # A typeable code is the common flow; prefer it when both are present.
        body = "Code 135790. Or click https://x.example.com/verify?t=z to confirm."
        assert imap_mcp.extract_verification(body) == "135790"

    def test_unrelated_number_ignored(self):
        # No verification keyword near the digits → not a code (avoid order #s etc).
        assert imap_mcp.extract_verification("Your order 482913 has shipped.") is None

    def test_over_long_number_not_sliced(self):
        # A 10-digit number (phone/account) near a keyword must NOT yield a
        # truncated 8-digit "code" — digit boundaries reject the whole run.
        assert imap_mcp.extract_verification("Your code 1234567890 follows") is None

    def test_no_code_or_link(self):
        assert imap_mcp.extract_verification("Welcome to the team — we're glad you're here.") is None


class TestFindVerification:
    def test_returns_code_from_most_recent(self):
        emails = [  # newest first
            {"subject": "Verify your email", "body": "Your code is 998877"},
            {"subject": "older", "body": "verification code 111111"},
        ]
        assert imap_mcp.find_verification_in(emails) == "998877"

    def test_skips_newest_without_a_code(self):
        emails = [
            {"subject": "Welcome", "body": "Thanks for joining!"},          # no code
            {"subject": "Verify", "body": "Your verification code: 424242"},
        ]
        assert imap_mcp.find_verification_in(emails) == "424242"

    def test_empty_returns_none(self):
        assert imap_mcp.find_verification_in([]) is None
