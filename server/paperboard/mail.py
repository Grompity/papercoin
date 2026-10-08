"""Email delivery — a provider boundary, never a pretend integration.

Mock mode (the default) prints the magic link to the console and remembers
it, so development can click the link without an inbox — the board stamps
the mail exactly like it stamps the mock feed. The real provider is stdlib
SMTP over environment variables only (no third-party deps); a configured
provider that cannot start raises at boot instead of quietly mocking real
money mail away.
"""

import smtplib
import ssl


class MockMail:
    """Dev mode. Always flags itself — nobody mistakes the console for an inbox."""

    mode = "mock"

    def __init__(self, settings):
        self.settings = settings
        self.sent = []                               # last links, for dev echo

    def send_magic_link(self, to, link_url):
        self.sent.append((to, link_url))
        del self.sent[:-25]                           # a ring, not a ledger
        print(f"[mail mock] → {to}: {link_url}")
        return True

    def last_token_for(self, to):
        """Dev convenience: the ring is the only place a plaintext token
        outlives the request that minted it. the database keeps only the
        hash, so a suppressed resend echo is answered from memory."""
        for email, link_url in reversed(self.sent):
            if email == to:
                return link_url.split("token=")[-1]
        return None


class SmtpMail:
    """Real delivery: stdlib SMTP, STARTTLS on the submission port (or
    implicit TLS on the SSL port). The password lives in the environment;
    nothing here is ever sent to the frontend."""

    mode = "smtp"

    def __init__(self, settings):
        cfg = settings.smtp
        if not cfg.get("host"):
            raise RuntimeError(
                "PAPER_SMTP_HOST missing — smtp mail is configured but cannot start")
        self.host = cfg["host"]
        self.port = int(cfg.get("port") or smtplib.SMTP_PORT)
        self.user = cfg.get("user") or ""
        self.password = cfg.get("password") or ""
        self.sender = (cfg.get("from") or cfg.get("user")
                       or "paperboard@localhost")
        self.from_name = cfg.get("fromName") or "PAPERBOARD"

    def send_magic_link(self, to, link_url):
        msg = (f"From: {self.from_name} <{self.sender}>\r\n"
               f"To: {to}\r\n"
               "Subject: your PAPERBOARD magic link\r\n"
               "Content-Type: text/plain; charset=utf-8\r\n"
               "\r\n"
               "Your PAPERBOARD magic link (single use, expires soon):\r\n"
               f"{link_url}\r\n\r\n"
               "You are receiving this because someone asked PAPERBOARD "
               "to sign an account in.\r\n")
        if self.port == smtplib.SMTP_SSL_PORT:
            conn = smtplib.SMTP_SSL(self.host, self.port, timeout=15,
                                    context=ssl.create_default_context())
        else:
            conn = smtplib.SMTP(self.host, self.port, timeout=15)
            conn.ehlo_or_helo_if_needed()
            try:
                conn.starttls(context=ssl.create_default_context())
            except smtplib.SMTPException as e:
                raise RuntimeError(f"smtp starttls refused: {e}")
        try:
            if self.user:
                conn.login(self.user, self.password)
            conn.sendmail(self.sender, [to], msg)
            conn.quit()
        except (smtplib.SMTPException, OSError) as e:
            raise RuntimeError(f"smtp send failed: {e}")
        return True


def make_mailer(settings):
    """Pick the provider from the environment (PAPER_EMAIL_PROVIDER).
    Anything unknown is a configuration mistake — say it at boot, loudly."""
    provider = getattr(settings, "email_provider", "mock")
    if provider == "mock":
        return MockMail(settings)
    if provider == "smtp":
        return SmtpMail(settings)
    raise RuntimeError(f"unknown PAPER_EMAIL_PROVIDER: {provider!r}")
