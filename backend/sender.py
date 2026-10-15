import smtplib
from html import escape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from . import config


def wrap_in_html(body, sender_name):
    paragraphs = body.split("\n\n")
    html_paragraphs = ""
    for p in paragraphs:
        if p.strip():
            cleaned = p.strip().replace("\n", "<br>")
            html_paragraphs += "<p>" + escape(cleaned).replace("&lt;br&gt;", "<br>") + "</p>"

    html = "<!DOCTYPE html>"
    html += "<html><head><meta charset='UTF-8'></head>"
    html += "<body style='font-family: Arial, sans-serif; color:#333; line-height:1.6; max-width:600px;'>"
    html += html_paragraphs
    html += "<hr style='border:none; border-top:1px solid #eee; margin:24px 0;'>"
    html += "<p style='font-size:12px; color:#999;'>"
    html += "If you don't want these emails, reply with unsubscribe."
    html += "</p></body></html>"
    return html


def send_email(to_email, subject, html_body, text_body=""):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((config.SENDER_NAME, config.GMAIL_USER))
    msg["To"] = to_email
    msg["Reply-To"] = config.GMAIL_USER
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid()
    msg["X-Mailer"] = "Email Automation"
    msg["List-Unsubscribe"] = "<mailto:" + config.GMAIL_USER + "?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    msg.attach(MIMEText(text_body or "Please view this message in an HTML-capable email client.", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
            server.sendmail(config.GMAIL_USER, to_email, msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        print("   [auth failed] Check App Password in .env")
        return False
    except Exception as e:
        print("   [send failed] " + str(e))
        return False