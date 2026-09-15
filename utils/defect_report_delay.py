"""Survey end-date and defect-report sent-date matching helpers."""

import base64
import re
import socket
import time
from datetime import date, datetime, timedelta

from googleapiclient.errors import HttpError

try:
	import holidays
except ImportError:  # pragma: no cover - deployment installs requirements.txt
	holidays = None


def indian_public_holidays(start_date, end_date):
	"""Return Indian public holidays in the inclusive date range."""
	if not start_date or not end_date:
		return set()
	if holidays is None:
		return {
			date(year, month, day)
			for year in range(start_date.year, end_date.year + 1)
			for month, day in ((1, 26), (8, 15), (10, 2))
			if start_date <= date(year, month, day) <= end_date
		}
	years = range(start_date.year, end_date.year + 1)
	return set(holidays.country_holidays("IN", years=years))


def working_days_between(start_date, end_date):
	"""Count Monday-Saturday dates, only excluding Sundays."""
	if not start_date or not end_date or end_date <= start_date:
		return 0

	current = start_date + timedelta(days=1)
	working_days = 0
	while current <= end_date:
		if current.weekday() != 6:
			working_days += 1
		current += timedelta(days=1)
	return working_days


def defect_report_delay_days(start_date, sent_date, allowed_days=3):
	"""Return delay days after the allowed Monday-Saturday working days."""
	return max(
		working_days_between(start_date, sent_date) - allowed_days,
		0,
	)


def _normalize(value):
	return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _header(headers, name):
	wanted = name.lower()
	for header in headers or []:
		if header.get("name", "").lower() == wanted:
			return header.get("value", "")
	return ""


def _is_retryable_gmail_error(exc):
	"""True for transient network / rate-limit Gmail API failures."""
	if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError)):
		return True
	if isinstance(exc, HttpError):
		return exc.resp.status in (429, 500, 502, 503, 504)
	return False


def _gmail_call(request, attempts=5):
	"""Execute a Gmail API request with backoff on transient failures."""
	last_exc = None
	for attempt in range(attempts):
		try:
			return request.execute()
		except Exception as exc:
			if not _is_retryable_gmail_error(exc):
				raise
			last_exc = exc
			print(
				f"[GMAIL] transient error, attempt {attempt + 1}/{attempts}: {exc}",
				flush=True,
			)
			if attempt < attempts - 1:
				time.sleep(1 + attempt * 2)
	raise last_exc


def _message_text(payload):
	parts = []
	body = payload.get("body", {}).get("data")
	if body:
		try:
			parts.append(base64.urlsafe_b64decode(body + "===").decode(
				"utf-8", errors="ignore"
			))
		except Exception:
			pass
	for child in payload.get("parts", []) or []:
		parts.append(_message_text(child))
	return " ".join(parts)


def _message_matches(survey, text):
	normalized_text = _normalize(text)
	required_parts = (
		survey.nh_number,
		survey.upc_code,
	)
	required_parts = tuple(part for part in required_parts if part)
	return all(_normalize(part) in normalized_text for part in required_parts)


def _message_matches_subject(survey, subject):
	return _message_matches(survey, subject)


def build_defect_email_index(gmail, survey=None):
	"""Read Sent defect-report subjects and index them by identifiers.

	When a survey is supplied, narrow Gmail's search to its identifiers so a
	request-triggered sync does not scan the entire Sent mailbox.
	"""
	matches = []
	page_token = None
	query = 'in:sent has:attachment subject:"Submission of Survey Report"'
	if survey:
		for identifier in (survey.nh_number, survey.upc_code):
			if identifier:
				query += ' "{}"'.format(str(identifier).replace('"', ''))
	while True:
		params = {
			"userId": "me",
			"q": query,
			"maxResults": 500,
		}
		if page_token:
			params["pageToken"] = page_token
		response = _gmail_call(gmail.users().messages().list(**params))

		for item in response.get("messages", []):
			message = _gmail_call(
				gmail.users().messages().get(
					userId="me",
					id=item["id"],
					format="metadata",
					metadataHeaders=["Subject"],
				)
			)
			headers = message.get("payload", {}).get("headers", [])
			subject = _header(headers, "Subject")
			internal_ms = int(message.get("internalDate", "0"))
			sent_at = datetime.utcfromtimestamp(internal_ms / 1000) if internal_ms else None
			if sent_at:
				matches.append({
					"subject": subject,
					"sent_at": sent_at,
					"message_id": message.get("id"),
				})

		page_token = response.get("nextPageToken")
		if not page_token:
			break

	return matches


def find_defect_report_email(survey, email_index, gmail=None):
	"""Return the earliest email carrying this survey's identifiers.

	The Gmail search query already restricts to messages containing the
	survey's identifiers, so a match is confirmed by checking the identifiers
	in the full body + subject rather than by a brittle stretch-line regex.
	The regex is kept only as a light cross-check; emails whose body layout
	differs (no "stretch no. ... - cycle" line) are still matched correctly.
	"""
	# With gmail available we can verify full bodies below, so every message
	# returned by the (identifier-narrowed) query is a candidate. Without gmail
	# we must fall back to the subject carrying the survey identifiers.
	subject_matches = [
		item for item in email_index
		if _message_matches(survey, item["subject"])
	]
	if gmail:
		exact_line = re.compile(
        r"stretch\s*no\.?\s*([a-z0-9./&()\s-]+?)\s*[-_]\s*cycle\s*([0-9]+)",
        re.IGNORECASE,
)
		candidates = []
		for item in email_index:
			try:
				message = _gmail_call(
					gmail.users().messages().get(
						userId="me",
						id=item["message_id"],
						format="full",
					)
				)
			except Exception:
				continue
			body_text = _message_text(message.get("payload", {}))
			full_text = body_text + " " + item["subject"]
			if not _message_matches(survey, full_text):
				continue
			body_match = exact_line.search(body_text)
			if body_match:
				section = _normalize(body_match.group(1))
				cycle = int(body_match.group(2))
				if section == _normalize(survey.section_no) and cycle != survey.cycle_no:
					continue
			candidates.append(item)
		matches = candidates
	else:
		matches = subject_matches
	if not matches:
		return None

	# Use the original defect-report email, not a later reply or reply-all.
	return min(matches, key=lambda item: item["sent_at"])
