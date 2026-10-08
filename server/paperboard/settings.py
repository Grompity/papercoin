"""Configuration + environment handling.

Everything tunable lives in server/config.json. Secrets (X client id/secret,
bearer, admin token, DB path, port) come from the environment only and are
NEVER sent to the frontend.
"""

import json
import os

SERVER_DIR = os.path.dirname(os.path.dirname(__file__))
SITE_DIR = os.path.dirname(SERVER_DIR)
CONFIG_PATH = os.path.join(SERVER_DIR, "config.json")


def _env(name, default=None):
    val = os.environ.get(name)
    if val is not None and val.strip() != "":
        return val.strip()
    return default


class Settings:
    """Runtime settings: raw config (public-safe) + env secrets (never public)."""

    def __init__(self):
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            self.raw = json.load(fh)

        self.port = int(_env("PAPER_PORT", "5199"))
        self.db_path = _env("PAPER_DB_PATH", os.path.join(SERVER_DIR, "paperboard.sqlite3"))
        self.admin_token = _env("PAPER_ADMIN_TOKEN")          # None until provided

        self.x_client_id = _env("X_CLIENT_ID")               # OAuth2 public client id
        self.x_client_secret = _env("X_CLIENT_SECRET")       # confidential, server-only
        self.x_bearer = _env("X_API_BEARER")                  # app-only read bearer
        self.x_redirect_uri = _env(
            "X_REDIRECT_URI", f"http://localhost:{self.port}/api/auth/x/callback"
        )

        # mock mode: explicit override, or auto = missing live credentials
        mock_env = _env("PAPER_MOCK")                         # "1"/"0"/None
        if mock_env is None:
            self.mock = not (self.x_client_id and self.x_bearer)
            self.mock_reason = (
                "missing X_CLIENT_ID / X_API_BEARER — running on the mock feed"
                if self.mock else "live X v2 feed"
            )
        else:
            self.mock = mock_env in ("1", "true", "yes")
            self.mock_reason = "forced by PAPER_MOCK"

        # scheduler on/off (cron-style: run `run.py --once` with PAPER_SCHEDULER=0)
        self.scheduler = _env("PAPER_SCHEDULER", "1") in ("1", "true", "yes")

        # cookie hardening: production sets PAPER_SECURE_COOKIES=1 so the
        # session cookie carries Secure alongside HttpOnly + SameSite=Lax.
        self.secure_cookies = _env("PAPER_SECURE_COOKIES", "0") in ("1", "true", "yes")

        # an inbox cannot follow a relative link — the magic URL that leaves
        # the server is absolute, built from what the browser will see.
        self.public_base = _env("PAPER_PUBLIC_BASE_URL", f"http://localhost:{self.port}")

        # email provider: mock (default) or smtp — the real one needs the env,
        # it never silently degrades to the mock (see mail.make_mailer).
        self.email_provider = _env("PAPER_EMAIL_PROVIDER", "mock")
        self.smtp = {
            "host": _env("PAPER_SMTP_HOST"),
            "port": _env("PAPER_SMTP_PORT"),
            "user": _env("PAPER_SMTP_USER"),
            "password": _env("PAPER_SMTP_PASSWORD"),
            "from": _env("PAPER_EMAIL_FROM"),
        }

    @property
    def pb(self):
        return self.raw["paperboard"]

    def public_view(self):
        """Everything the frontend may see. No secrets, no credential-shaped surprises."""
        pb = self.pb
        return {
            "mode": "mock" if self.mock else "live",
            "modeReason": self.mock_reason,
            "brand": self.raw["brand"],
            "refreshMinutes": pb["refreshMinutes"],
            "schedulerOn": self.scheduler,
            "eligibility": pb["eligibility"],
            "scoring": pb["scoring"],
            "prize": pb["prize"],
            "competition": {
                k: pb["competition"][k]
                for k in ("slug", "name", "description", "startsAt", "endsAt", "status")
            },
            "tiers": pb["tiers"],
            # the account layer's printed rules (timings only — never secrets)
            "account": pb.get("account", {}),
            "emailMode": self.email_provider,
        }
