import logging
import os
from typing import Optional

_LOG = logging.getLogger(__name__)

def _wrap_email_html(*, heading: str, body_html: str, brand: str = "TelePsych") -> str:
    """
    Basic responsive transactional email wrapper.
    Uses table layout + inline styles for broad email client support.
    """
    # Palette roughly aligned with site (warm brown + cream)
    brand_bg = "#7A5C3B"
    page_bg = "#F8F5EF"
    card_bg = "#FFFFFF"
    text = "#1F2937"
    muted = "#6B7280"
    border = "#E6DDCF"

    return f"""\
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="x-apple-disable-message-reformatting" />
    <title>{brand}</title>
  </head>
  <body style="margin:0;padding:0;background:{page_bg};">
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:{page_bg};padding:24px 0;">
      <tr>
        <td align="center">
          <table role="presentation" cellpadding="0" cellspacing="0" width="600" style="width:600px;max-width:600px;">
            <tr>
              <td style="background:{brand_bg};padding:18px 22px;border-radius:14px 14px 0 0;">
                <div style="font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:18px;font-weight:700;color:#ffffff;letter-spacing:0.2px;">
                  {brand}
                </div>
              </td>
            </tr>
            <tr>
              <td style="background:{card_bg};border:1px solid {border};border-top:none;padding:22px;border-radius:0 0 14px 14px;">
                <div style="font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:{text};">
                  <h1 style="margin:0 0 12px 0;font-size:22px;line-height:1.25;color:{text};">{heading}</h1>
                  <div style="font-size:14px;line-height:1.6;color:{text};">
                    {body_html}
                  </div>
                  <div style="margin-top:18px;font-size:12px;line-height:1.6;color:{muted};">
                    If you did not expect this email, you can safely ignore it.
                  </div>
                </div>
              </td>
            </tr>
            <tr>
              <td style="padding:14px 6px 0 6px;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:12px;color:{muted};text-align:center;">
                © {brand}
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def _button(href: str, label: str) -> str:
    """CTA button with fallback-friendly styling."""
    btn_bg = "#7A5C3B"
    return f"""\
<div style="margin:18px 0;">
  <a href="{href}" style="display:inline-block;background:{btn_bg};color:#ffffff;text-decoration:none;padding:12px 16px;border-radius:10px;font-weight:700;">
    {label}
  </a>
</div>
"""


def _is_production() -> bool:
    env = os.environ.get("ENV", "").lower()
    if env in ("production", "prod"):
        return True
    return (os.environ.get("FRONTEND_ORIGIN") or "").strip().lower().startswith("https://")


def _send_email(to: str, subject: str, html: str) -> tuple[bool, Optional[str]]:
    """Send one email via Resend. Returns (True, None) on success, (False, error_message) on failure."""
    # Read from env at send time so values are correct after env load
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    from_addr = (os.environ.get("EMAIL_FROM") or "").strip()

    if not api_key:
        if _is_production():
            _LOG.warning("Email skipped: RESEND_API_KEY not set (to=%s, subject=%s)", to, subject)
        else:
            _LOG.info("Email skipped (no Resend key): to=%s, subject=%s", to, subject)
        return True, None  # dev: allow approval without email
    if not from_addr:
        _LOG.warning("Email skipped: EMAIL_FROM not set (to=%s, subject=%s)", to, subject)
        return False, "EMAIL_FROM not set in environment"

    try:
        import resend
        resend.api_key = api_key
        r = resend.Emails.send({
            "from": from_addr,
            "to": [to],
            "subject": subject,
            "html": html,
            "click_tracking": False,  # Disable click tracking to avoid Spectrum/ISP security blocks
        })
        # Resend returns dict with "id" on success; SDK may also return object with .id
        email_id = r.get("id") if isinstance(r, dict) else getattr(r, "id", None)
        if email_id:
            _LOG.info("Email sent via Resend (id=%s, to=%s, subject=%s)", email_id, to, subject)
            return True, None
        msg = (r.get("message") if isinstance(r, dict) else getattr(r, "message", None)) or str(r)
        _LOG.warning("Resend returned no id (to=%s, subject=%s): %s", to, subject, msg)
        return False, msg or "Resend returned no email id"
    except Exception as e:
        _LOG.exception("Resend send failed (to=%s, subject=%s): %s", to, subject, e)
        return False, str(e)


def send_acceptance_message_email(to: str, first_name: str) -> tuple[bool, Optional[str]]:
    """Send the practice acceptance message (good fit, next steps). Sent when doctor approves."""
    html = f"""
    <p>Dear Parent or Guardian,</p>
    <p>Thank you for completing the pre-screening questionnaire. Based on the information
    provided, your child appears to be an appropriate fit for the scope of services offered by
    our practice.</p>
    <p>Our practice provides integrated outpatient psychiatric care for children,
    adolescents, and young adults ages 5 through 25, including medication management,
    psychotherapy, and parent support, delivered exclusively via telehealth.</p>
    <p>The next steps include:</p>
    <ul>
        <li>Completion of required intake and consent documents</li>
        <li>Review of practice policies and fee structure</li>
        <li>Scheduling of the initial evaluation appointment</li>
    </ul>
    <p>Please note that a physician–patient relationship is established only after all
    documentation is completed and the initial appointment has occurred.</p>
    <p>If you would like to proceed, we will send you a secure link with onboarding and
    scheduling instructions.</p>
    <p>Sincerely,<br/>
    Child Psychiatry Services<br/>
    Madhuri Medaramelta, MD<br/>
    Child &amp; Adolescent Psychiatry<br/>
    Telehealth Practice</p>
    """
    return _send_email(to, "Your inquiry — next steps", html)


def send_rejection_email(to: str, first_name: str, rejected_note: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Send the non-fit / referral message when doctor rejects the request."""
    extra = (
        f"""<div style="margin-top:14px;padding:12px 14px;border:1px solid #E6DDCF;border-radius:12px;background:#FBF9F5;">
        <div style="font-weight:700;margin-bottom:6px;">Note from the practice</div>
        <div>{rejected_note}</div>
      </div>"""
        if rejected_note
        else ""
    )

    body = f"""
<p>Dear Parent or Guardian,</p>
<p>
  Thank you for taking the time to complete our pre-screening questionnaire. After careful review,
  we have determined that our practice would not be the most appropriate setting for your child's current needs.
</p>
<p>
  This decision is based on clinical scope and practice limitations rather than any judgment about the importance
  or validity of your concerns. At this time, your child may benefit from services that provide a different level
  or type of support than we are able to offer through our telehealth-only, outpatient model.
</p>
<p>We strongly encourage you to seek care through one or more of the following options:</p>
<ul style="margin:10px 0 0 18px;padding:0;">
  <li style="margin:6px 0;">Your child's primary care provider for coordination of referrals</li>
  <li style="margin:6px 0;">A local child psychiatrist or behavioral health clinic</li>
  <li style="margin:6px 0;">Community mental health services or hospital-affiliated programs</li>
</ul>
<div style="margin-top:14px;padding:12px 14px;border:1px solid #F3D8A4;border-radius:12px;background:#FFF7E6;">
  <div style="font-weight:700;margin-bottom:6px;">Important</div>
  <div>If your child is experiencing worsening symptoms or any safety concerns, please contact emergency services or proceed to the nearest emergency department.</div>
</div>
<p style="margin-top:14px;">We appreciate your understanding and wish you and your family the very best moving forward.</p>
{extra}
<p style="margin-top:16px;">
  Sincerely,<br/>
  Child Psychiatry Services<br/>
  Madhuri Medaramelta, MD<br/>
  Child &amp; Adolescent Psychiatry<br/>
  Telehealth Practice
</p>
"""
    html = _wrap_email_html(
        heading="Regarding your inquiry",
        body_html=body,
    )
    return _send_email(to, "Regarding Your Inquiry for Psychiatric Services", html)


def send_approval_email(
    to: str,
    first_name: str,
    reset_token: str,
) -> tuple[bool, Optional[str]]:
    """Send account setup email (set password link)."""
    from urllib.parse import quote
    app_url = (os.environ.get("APP_URL") or "").strip()
    if not app_url:
        _LOG.warning("APP_URL not set; reset link in approval email may be broken")
    reset_link = f"{app_url or 'https://example.com'}/reset-password?token={quote(reset_token)}"
    body = f"""
<p>Hello {first_name},</p>
<p>Your request for access to TelePsych has been approved.</p>
<p>To finish setting up your account, please create your password using the secure link below:</p>
{_button(reset_link, "Set Password")}
<div style="margin-top:10px;padding:12px 14px;border:1px solid #E6DDCF;border-radius:12px;background:#FBF9F5;">
  <div style="font-weight:700;margin-bottom:6px;">Or copy this link</div>
  <div style="word-break:break-all;"><a href="{reset_link}" style="color:#7A5C3B;">{reset_link}</a></div>
</div>
<div style="margin-top:14px;padding:12px 14px;border:1px solid #F3D8A4;border-radius:12px;background:#FFF7E6;">
  <div style="font-weight:700;margin-bottom:6px;">Important</div>
  <div>This link expires in 24 hours for your security.</div>
</div>
<p style="margin-top:16px;">Best regards,<br/>{'TelePsych'}</p>
"""
    html = _wrap_email_html(
        heading="Your access is approved",
        body_html=body,
    )
    return _send_email(to, "TelePsych — Your access has been approved", html)


def send_appointment_confirmation_email(
    *,
    to: str,
    patient_first_name: str,
    scheduled_at_iso: str,
    meet_link: str | None,
) -> tuple[bool, Optional[str]]:
    """Send appointment confirmation with date/time and Meet link (when patient books)."""
    # Format date/time for display in Pacific Time
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        
        PRACTICE_TZ = ZoneInfo("America/Los_Angeles")
        dt_utc = datetime.fromisoformat(scheduled_at_iso.replace("Z", "+00:00"))
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=ZoneInfo("UTC"))
        dt_pacific = dt_utc.astimezone(PRACTICE_TZ)
        
        # Get timezone abbreviation (PST or PDT)
        tz_abbr = dt_pacific.strftime("%Z")
        if not tz_abbr or tz_abbr == dt_pacific.strftime("%z"):
            # Fallback if %Z doesn't work
            tz_abbr = "PT"
        
        # Show in a readable form (e.g. "Jan 30, 2025 at 10:00 AM PST")
        date_str = dt_pacific.strftime("%b %d, %Y at %I:%M %p") + f" {tz_abbr}"
    except Exception:
        date_str = scheduled_at_iso
    meet_section = ""
    if meet_link:
        meet_section = f"""
<div style="margin-top:14px;padding:12px 14px;border:1px solid #E6DDCF;border-radius:12px;background:#FBF9F5;">
  <div style="font-weight:700;margin-bottom:6px;">Join your video visit</div>
  <p style="margin:0 0 10px 0;">Use this link at your appointment time:</p>
  {_button(meet_link, "Join video call")}
  <div style="margin-top:8px;word-break:break-all;"><a href="{meet_link}" style="color:#7A5C3B;">{meet_link}</a></div>
</div>
"""
    else:
        meet_section = """
<div style="margin-top:14px;padding:12px 14px;border:1px solid #F3D8A4;border-radius:12px;background:#FFF7E6;">
  <div style="font-weight:700;margin-bottom:6px;">Video link</div>
  <p style="margin:0;">Your provider will send the video visit link separately, or you can find it in your patient portal.</p>
</div>
"""
    body = f"""
<p>Hello {patient_first_name},</p>
<p>Your appointment has been confirmed.</p>
<div style="margin:14px 0;padding:12px 14px;border:1px solid #E6DDCF;border-radius:12px;background:#FBF9F5;">
  <div style="font-weight:700;margin-bottom:6px;">Date & time</div>
  <div>{date_str}</div>
</div>
{meet_section}
<p style="margin-top:16px;">Best regards,<br/>TelePsych</p>
"""
    html = _wrap_email_html(
        heading="Appointment confirmed",
        body_html=body,
    )
    return _send_email(to, "TelePsych — Appointment confirmed", html)


def send_password_reset_email(
    *,
    to: str,
    first_name: str,
    reset_token: str,
    portal: str = "doctor",
    expires_hours: int = 24,
) -> tuple[bool, Optional[str]]:
    """Send a password reset email (e.g. for doctors)."""
    from urllib.parse import quote

    app_url = (os.environ.get("APP_URL") or "").strip()
    if not app_url:
        _LOG.warning("APP_URL not set; reset link in password reset email may be broken")

    next_param = "doctor" if portal == "doctor" else "patient"
    reset_link = f"{app_url or 'https://example.com'}/reset-password?token={quote(reset_token)}&next={next_param}"

    body = f"""
<p>Hello {first_name},</p>
<p>We received a request to reset your password.</p>
{_button(reset_link, "Reset Password")}
<div style="margin-top:10px;padding:12px 14px;border:1px solid #E6DDCF;border-radius:12px;background:#FBF9F5;">
  <div style="font-weight:700;margin-bottom:6px;">Or copy this link</div>
  <div style="word-break:break-all;"><a href="{reset_link}" style="color:#7A5C3B;">{reset_link}</a></div>
</div>
<div style="margin-top:14px;padding:12px 14px;border:1px solid #F3D8A4;border-radius:12px;background:#FFF7E6;">
  <div style="font-weight:700;margin-bottom:6px;">Important</div>
  <div>This link expires in {expires_hours} hours for your security.</div>
</div>
<p style="margin-top:16px;">Best regards,<br/>{'TelePsych'}</p>
"""

    html = _wrap_email_html(
        heading="Reset your password",
        body_html=body,
    )
    return _send_email(to, "TelePsych — Reset your password", html)
