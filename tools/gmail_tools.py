import smtplib
from email.message import EmailMessage


def send_gmail(to: str, subject: str, body: str, sender: str, app_password: str) -> None:
    """Send a plain-text email through Gmail SMTP."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(sender, app_password)
        smtp.send_message(message)
