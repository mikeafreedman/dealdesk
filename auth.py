import random
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request, HTTPException
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from auth_config import (
    APPROVED_EMAILS,
    TRUSTED_EMAILS,
    REMEMBER_ME_DAYS,
    GMAIL_SENDER,
    GMAIL_APP_PASSWORD,
    SESSION_SECRET,
)

logger = logging.getLogger(__name__)

# ── Serializer for signed session cookies ────────────────
serializer = URLSafeTimedSerializer(SESSION_SECRET)

# ── In-memory OTP store: { email: { code, expires_at } } ─
_otp_store: dict = {}

OTP_EXPIRY_MINUTES = 10
SESSION_COOKIE = "dealdesk_session"


def generate_otp(email: str) -> str:
    """Generate a 6-digit OTP, store it, return it."""
    code = str(random.randint(100000, 999999))
    _otp_store[email.lower()] = {
        "code": code,
        "expires_at": datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINUTES),
    }
    logger.info(f"AUTH: OTP generated for {email}")
    return code


def verify_otp(email: str, code: str) -> bool:
    """Return True if code matches and has not expired."""
    email = email.lower()
    record = _otp_store.get(email)
    if not record:
        logger.warning(f"AUTH: No OTP found for {email}")
        return False
    if datetime.utcnow() > record["expires_at"]:
        logger.warning(f"AUTH: OTP expired for {email}")
        del _otp_store[email]
        return False
    if record["code"] != code.strip():
        logger.warning(f"AUTH: Wrong OTP for {email}")
        return False
    del _otp_store[email]
    logger.info(f"AUTH: OTP verified for {email}")
    return True


def is_approved(email: str) -> bool:
    """Return True if email is in the approved list."""
    return email.lower() in [e.lower() for e in APPROVED_EMAILS]


def is_trusted(email: str) -> bool:
    """Return True if email is allowed to use Remember This Device."""
    return email.lower() in [e.lower() for e in TRUSTED_EMAILS]


def send_otp_email(email: str, code: str) -> bool:
    """Send OTP to user via Gmail. Return True on success."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Your DealDesk Access Code"
        msg["From"] = f"DealDesk <{GMAIL_SENDER}>"
        msg["To"] = email

        html = f"""
        <html>
        <body style="margin:0;padding:0;background:#1a1a1a;
                     font-family:'Century Gothic',sans-serif;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td align="center" style="padding:48px 24px;">
                <table width="480" cellpadding="0" cellspacing="0"
                       style="background:#2C1F14;border-radius:12px;
                              overflow:hidden;">
                  <tr>
                    <td style="background:#2C1F14;padding:32px 40px 24px;
                                border-bottom:1px solid #4A6E50;">
                      <p style="margin:0;font-size:22px;font-weight:700;
                                 color:#F5EFE4;letter-spacing:1px;">
                        DEALDESK
                      </p>
                      <p style="margin:4px 0 0;font-size:12px;
                                 color:#7A9E7E;letter-spacing:2px;">
                        FREEDMAN PROPERTIES
                      </p>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:40px 40px 32px;">
                      <p style="margin:0 0 8px;font-size:14px;
                                 color:#C4A882;">
                        Your one-time access code is:
                      </p>
                      <p style="margin:0 0 32px;font-size:48px;
                                 font-weight:700;letter-spacing:12px;
                                 color:#F5EFE4;">
                        {code}
                      </p>
                      <p style="margin:0;font-size:13px;color:#8B7355;
                                 line-height:1.6;">
                        This code expires in {OTP_EXPIRY_MINUTES} minutes.<br>
                        If you did not request this code, ignore this email.
                      </p>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:20px 40px;
                                border-top:1px solid #3d2e1e;">
                      <p style="margin:0;font-size:11px;color:#5C3D26;">
                        DealDesk &bull; Authorized Access Only
                      </p>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </body>
        </html>
        """

        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_SENDER, email, msg.as_string())

        logger.info(f"AUTH: OTP email sent to {email}")
        return True

    except Exception as e:
        logger.error(f"AUTH: Failed to send OTP email to {email}: {e}")
        return False


def create_session_token(email: str) -> str:
    """Create a signed session token for the email."""
    return serializer.dumps({"email": email.lower()})


def get_current_user(request: Request) -> Optional[str]:
    """
    Read and verify the session cookie.
    Returns the email string if valid, None otherwise.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=None)
        email = data.get("email", "")
        if not is_approved(email):
            return None
        return email
    except (BadSignature, SignatureExpired, Exception):
        return None


def require_auth(request: Request) -> str:
    """
    Dependency for protected routes.
    Raises 401 if not authenticated.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
