'''Email sending via SMTP.

Used for: trial key delivery.
All emails are transactional - no marketing without explicit opt-in.
'''

import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

SMTP_HOST = os.environ['SMTP_HOST']
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ['SMTP_USER']
SMTP_PASSWORD = os.environ['SMTP_PASSWORD']
SMTP_FROM = os.environ['SMTP_FROM']
BASE_URL = os.environ.get('BASE_URL', '')
TRIAL_TOKENS = int(os.environ.get('TRIAL_TOKENS', '1000000'))
TRIAL_EXPIRY_DAYS = int(os.environ.get('TRIAL_EXPIRY_DAYS', '7'))


async def send_trial_key_email(to_email: str, api_key: str) -> None:
    '''Send the trial API key to a new registrant via SMTP.

    Args:
        to_email: Recipient email address.
        api_key: The raw API key to include in the email (shown only once).
    '''

    if TRIAL_TOKENS >= 1_000_000:
        trial_tokens_fmt = f"{TRIAL_TOKENS / 1_000_000:.1f}M"
    elif TRIAL_TOKENS >= 1_000:
        trial_tokens_fmt = f"{TRIAL_TOKENS // 1_000}k"
    else:
        trial_tokens_fmt = str(TRIAL_TOKENS)

    subject = "Your API key: get started in 60 seconds"

    text_body = f"""Welcome!

Your API key is:

    {api_key}

Keep it safe; it won't be shown again.

You have {trial_tokens_fmt} free tokens to use within the next {TRIAL_EXPIRY_DAYS} days.

To make your first request:

    curl {BASE_URL}/v1/chat/completions \\
      -H "Authorization: Bearer {api_key}" \\
      -H "Content-Type: application/json" \\
      -d '{{
        "model": "default",
        "messages": [{{"role": "user", "content": "Hello!"}}]
      }}'

View your usage and balance:
    {BASE_URL}/dashboard?key={api_key}

Questions? Just reply to this email.
"""

    html_body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 560px; margin: 40px auto; color: #222; line-height: 1.6; }}
    code, pre {{ background: #f4f4f4; border-radius: 4px; padding: 2px 6px; font-family: monospace; }}
    pre {{ padding: 16px; overflow-x: auto; }}
    .key {{ font-size: 1.1em; font-weight: bold; letter-spacing: 0.02em; }}
    .note {{ color: #666; font-size: 0.9em; }}
    a {{ color: #2563eb; }}
  </style>
</head>
<body>
  <p>Welcome!</p>

  <p>Your API key is:</p>
  <pre class="key">{api_key}</pre>
  <p class="note">Keep it safe; it won't be shown again.</p>

  <p>
    You have <strong>{trial_tokens_fmt} free tokens</strong>
    to use within the next <strong>{TRIAL_EXPIRY_DAYS} days</strong>.
  </p>

  <p>Make your first request:</p>
  <pre>curl {BASE_URL}/v1/chat/completions \\
  -H "Authorization: Bearer {api_key}" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "model": "default",
    "messages": [{{"role": "user", "content": "Hello!"}}]
  }}'</pre>

  <p>
    <a href="{BASE_URL}/dashboard?key={api_key}">View your usage and balance →</a>
  </p>

  <p class="note">Questions? Reply to this email.</p>
</body>
</html>"""

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = SMTP_FROM
    msg['To'] = to_email
    msg.attach(MIMEText(text_body, 'plain'))
    msg.attach(MIMEText(html_body, 'html'))

    await aiosmtplib.send(
        msg,
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        username=SMTP_USER,
        password=SMTP_PASSWORD,
        start_tls=True,
    )
