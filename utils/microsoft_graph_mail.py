"""Send mail through Microsoft Graph using app-only credentials."""

import base64
import mimetypes
import os
from urllib.parse import quote

import requests


TOKEN_TIMEOUT_SECONDS = 20
SEND_TIMEOUT_SECONDS = 30
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


def _raise_for_service_error(response, service_name):
    if response.ok:
        return

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    error = payload.get("error") or {}
    if isinstance(error, dict):
        error_code = error.get("code") or "unknown_error"
        error_message = error.get("message") or "No error details returned"
    else:
        error_code = str(error)
        error_message = payload.get("error_description") or "No error details returned"

    request_id = response.headers.get("request-id")
    request_detail = f"; request-id={request_id}" if request_id else ""
    raise RuntimeError(
        f"{service_name} request failed with HTTP {response.status_code} "
        f"({error_code}): {error_message}{request_detail}"
    )


def send_microsoft_graph_email(
    to_email,
    from_email,
    subject,
    html_body,
    attachment_bytes,
    attachment_filename,
    message_id=None,
    additional_attachments=None,
):
    """Send an HTML email and its attachments from the configured M365 mailbox."""
    tenant_id = (os.getenv("M365_TENANT_ID") or "").strip()
    client_id = (os.getenv("M365_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("M365_CLIENT_SECRET") or "").strip()
    if not all((tenant_id, client_id, client_secret)):
        raise RuntimeError("Microsoft Graph mail credentials are not configured")

    token_response = requests.post(
        f"https://login.microsoftonline.com/{quote(tenant_id, safe='')}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=TOKEN_TIMEOUT_SECONDS,
    )
    _raise_for_service_error(token_response, "Microsoft identity")
    access_token = token_response.json().get("access_token")
    if not access_token:
        raise RuntimeError("Microsoft identity response did not include an access token")

    attachments = [(attachment_bytes, attachment_filename)]
    attachments.extend(additional_attachments or [])
    graph_attachments = []
    for content, filename in attachments:
        graph_attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename,
                "contentType": (
                    mimetypes.guess_type(filename)[0]
                    or "application/octet-stream"
                ),
                "contentBytes": base64.b64encode(content).decode("ascii"),
            }
        )

    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
        "attachments": graph_attachments,
    }
    if message_id:
        message["internetMessageHeaders"] = [
            {
                "name": "X-VIAMA-Message-ID",
                "value": message_id.strip("<>")
            }
        ]

    response = requests.post(
        f"{GRAPH_ROOT}/users/{quote(from_email, safe='')}/sendMail",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"message": message, "saveToSentItems": True},
        timeout=SEND_TIMEOUT_SECONDS,
    )
    _raise_for_service_error(response, "Microsoft Graph sendMail")
    if response.status_code != 202:
        raise RuntimeError(
            f"Microsoft Graph returned unexpected send status {response.status_code}"
        )

    return message_id or response.headers.get("request-id") or "accepted"