from __future__ import annotations

import html
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..config import EmailConfig
from ..models import SearchResult
from ..utils import safe_url


def render_email_html(results: list[SearchResult], person_name: str, run_summary: str = "") -> str:
    def esc(value: object) -> str:
        return html.escape("" if value is None else str(value))

    def esc_href(value: object) -> str:
        return html.escape("" if value is None else str(value), quote=True)

    rows = []
    for index, result in enumerate(sorted(results, key=lambda r: r.name.lower()), start=1):
        rows.append(
            f"""
            <tr>
                <td>{index}</td>
                <td>{esc(result.name)}</td>
                <td>{esc(result.ebay_site)}</td>
                <td>{esc(result.price)}</td>
                <td>{esc(result.seller)}</td>
                <td>{esc(result.starts)}</td>
                <td>{esc(result.ends)}</td>
                <td>{esc(result.keywords)}</td>
                <td><a href="{esc_href(safe_url(result.link))}">Link</a></td>
            </tr>
            """
        )

    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            table {{
                width: 100%;
                border-collapse: collapse;
            }}
            th, td {{
                border: 1px solid #ddd;
                padding: 8px;
                text-align: left;
            }}
            th {{
                background-color: #f2f2f2;
            }}
        </style>
    </head>
    <body>
        <p>Hello {esc(person_name)},</p>
        <p>{esc(run_summary)}</p>
        <p><b>{len(results)}</b> items awaiting notification:</p>
        <table>
            <tr>
                <th>#</th>
                <th>Name</th>
                <th>Ebay Site</th>
                <th>Price</th>
                <th>Seller</th>
                <th>Starts</th>
                <th>Ends</th>
                <th>Keywords</th>
                <th>Link</th>
            </tr>
            {"".join(rows)}
        </table>
        <p>Best regards,<br>Your eBay Search Bot</p>
    </body>
    </html>
    """


class EmailNotifier:
    def __init__(self, config: EmailConfig, logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("fastebaysearch")

    def send(self, results: list[SearchResult], run_summary: str = "") -> list[SearchResult]:
        if not results:
            return []
        results = results[: self.config.max_per_run]

        subject = f"[{len(results)} NEW] {self.config.subject}"
        message = MIMEMultipart()
        message["From"] = self.config.sender
        message["To"] = self.config.receiver
        message["Subject"] = subject
        message.attach(MIMEText(render_email_html(results, self.config.person_name, run_summary), "html", "utf-8"))

        try:
            with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port, timeout=10) as server:
                if self.config.starttls:
                    server.starttls(context=ssl.create_default_context())
                if self.config.authenticate:
                    server.login(self.config.smtp_login, self.config.smtp_password)
                server.send_message(message)
                self.logger.info(f"Email sent: {subject}")
                return results
        except smtplib.SMTPAuthenticationError:
            self.logger.error("SMTP authentication failed; check username and password.")
        except (OSError, smtplib.SMTPException) as exc:
            self.logger.error(f"SMTP error: {exc}")
        return []
