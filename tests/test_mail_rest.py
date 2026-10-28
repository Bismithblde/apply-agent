import base64
import logging
import time

import pytest

from agent import db, llm, mail, net


@pytest.fixture
def mail_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mail.db")
    db.init()
    monkeypatch.setattr(llm, "available", lambda: False)
    monkeypatch.setattr(mail, "POLL_SECONDS", 0.002)


def test_gmail_rest_uses_agent_net(monkeypatch):
    calls = []

    def fake_request(method, url, *, headers=None, timeout=20, **kwargs):
        calls.append((method, url, headers, timeout, kwargs))
        return 200, '{"emailAddress":"test@example.com"}'

    monkeypatch.setattr(mail, "_access_token", lambda: "local-test-token")
    monkeypatch.setattr(net, "request", fake_request)

    assert mail._gapi("profile") == {"emailAddress": "test@example.com"}
    assert calls == [("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                      {"Authorization": "Bearer local-test-token"}, mail.NET_TIMEOUT, {})]


def test_missing_gmail_oauth_does_not_try_imap(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(tmp_path / "missing-token.json"))
    monkeypatch.setenv("GMAIL_CLIENT_SECRET_FILE", str(tmp_path / "missing-client.json"))
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "local-fake-password")
    monkeypatch.setattr(mail, "_backend_cache", (0.0, None))
    attempts = []
    monkeypatch.setattr(mail, "_imap_ok", lambda: attempts.append(True) or True, raising=False)

    assert mail.available() is False
    assert mail.wait_for_verification(time.time(), timeout=0.1) is None
    assert attempts == []


def test_wait_reads_gmail_verification_and_marks_seen(mail_db, monkeypatch):
    now = time.time()
    msg = mail._Msg(id="gmail-rest-1", subject="Verification code", sender="jobs@example.com",
                    to="jane+test@gmail.com",
                    ts=now, text="Your verification code is 638241.", html="")
    queries = []

    monkeypatch.setattr(mail, "_gmail_list", lambda query: queries.append(query) or [msg])
    monkeypatch.setattr(llm, "available", lambda: False)

    result = mail.wait_for_verification(now - 5, sender_hint="example.com", to_alias="jane+test@gmail.com",
                                        timeout=1, want="code")

    assert result == mail.Verification(code="638241", link=None, message_id="gmail-rest-1",
                                       subject="Verification code", sender="jobs@example.com")
    assert queries == ["after:" + str(int(now - 5)) + " from:example.com to:jane+test@gmail.com"]
    assert db.mail_is_seen("gmail-rest-1")


def test_incomplete_entry_can_retry_until_acknowledged(mail_db, monkeypatch, caplog):
    now = time.time()
    _mock_gmail(monkeypatch, [_message("deferred", now), _message("superseded", now - 1)],
                strict_empty=True)

    def retrieve():
        return mail.wait_for_verification(now - 5, sender_hint="oracle", to_alias="jane.doe@gmail.com",
                                          timeout=0.02, want="code", consume=False)

    with caplog.at_level(logging.INFO, logger="agent.mail"):
        first = retrieve()
        assert first and first.message_id == "deferred"
        assert not db.mail_is_seen(first.message_id)
        assert retrieve() == first
        mail.acknowledge_verification(first, want="code")
        mail.acknowledge_verification(first, want="code")
        assert retrieve() is None

    assert db.mail_is_seen("deferred") and not db.mail_is_seen("superseded")
    with db.connect() as c:
        rows = c.execute("SELECT message_id, purpose FROM mail_seen").fetchall()
    assert [(r["message_id"], r["purpose"]) for r in rows] == [("deferred", "verification:code")]
    assert "consumed=False" in caplog.text and "'consumed':" in caplog.text
    assert first.code not in caplog.text and "jane.doe@gmail.com" not in caplog.text


def test_deferred_retry_rechecks_expiry(mail_db, monkeypatch):
    now = time.time()
    clock = [now]
    monkeypatch.setattr(mail.time, "time", lambda: clock[0])
    _mock_gmail(monkeypatch, [_message("short-lived", now,
                body="Your verification code is 638241. It expires in 1 second.")])

    assert mail.wait_for_verification(now - 5, to_alias="jane.doe@gmail.com", timeout=0.02,
                                      want="code", consume=False)
    clock[0] += 2
    assert mail.wait_for_verification(now - 5, to_alias="jane.doe@gmail.com", timeout=0.02,
                                      want="code", consume=False) is None
    assert not db.mail_is_seen("short-lived")


def test_deferred_retry_respects_resend_timestamp(mail_db, monkeypatch):
    now = time.time()
    messages = [_message("first-request", now - 3)]
    _mock_gmail(monkeypatch, messages, strict_empty=True)

    first = mail.wait_for_verification(now - 5, sender_hint="oracle", to_alias="jane.doe@gmail.com",
                                        timeout=0.02, want="code", consume=False)
    assert first and first.message_id == "first-request"
    assert mail.wait_for_verification(now - 2, sender_hint="oracle", to_alias="jane.doe@gmail.com",
                                      timeout=0.02, want="code", consume=False) is None
    messages.append(_message("resend", now - 1))
    second = mail.wait_for_verification(now - 2, sender_hint="oracle", to_alias="jane.doe@gmail.com",
                                         timeout=0.02, want="code", consume=False)

    assert second and second.message_id == "resend"
    assert not db.mail_is_seen("first-request") and not db.mail_is_seen("resend")


def test_deferred_retry_prefers_newest_message_without_a_resend_timestamp(mail_db, monkeypatch):
    now = time.time()
    messages = [_message("first", now - 1)]
    _mock_gmail(monkeypatch, messages)

    first = mail.wait_for_verification(now - 5, to_alias="jane.doe@gmail.com", timeout=0.02,
                                       want="code", consume=False)
    assert first and first.message_id == "first"
    messages.append(_message("replacement", now))
    second = mail.wait_for_verification(now - 5, to_alias="jane.doe@gmail.com", timeout=0.02,
                                        want="code", consume=False)

    assert second and second.message_id == "replacement"
    assert not db.mail_is_seen("first") and not db.mail_is_seen("replacement")


def test_verification_repr_omits_code_and_token_link():
    verification = mail.Verification("638241", "https://example.test/verify?token=private-test-token",
                                     "message", "Your verification code is 638241", "careers@example.test")
    assert "638241" not in repr(verification)
    assert "private-test-token" not in repr(verification)


def _message(message_id, ts, *, sender="ADT Careers <careers@notifications.employer.example>",
             recipient="jane.doe@gmail.com", subject="Confirm your identity", thread="identity-thread",
             body="<p>Please confirm your identity using the one-time pass code: <b>638241</b>.</p>"
                  "<p>This code will expire in 10 minutes.</p>", delivered_to=None):
    headers = [{"name": "Subject", "value": subject}, {"name": "From", "value": sender},
               {"name": "To", "value": recipient}]
    if delivered_to:
        headers.append({"name": "Delivered-To", "value": delivered_to})
    return {"id": message_id, "threadId": thread, "internalDate": str(int(ts * 1000)),
            "payload": {"mimeType": "text/html", "headers": headers,
                        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")}}}


def _mock_gmail(monkeypatch, messages, *, strict_empty=False):
    queries = []

    def fake_gapi(path, **params):
        if path == "messages":
            queries.append(params["q"])
            matches = [] if strict_empty and "from:" in params["q"] else messages
            assert params["includeSpamTrash"] is True
            return {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in matches]}
        assert path.startswith("messages/")
        return next(m for m in messages if m["id"] == path.split("/", 1)[1])

    monkeypatch.setattr(mail, "_gapi", fake_gapi)
    return queries


@pytest.mark.parametrize("sender,subject", [
    ("ADT Careers <ADTCareers@horizon.mail.adt.com>", "Confirm your identity"),
    ("Human Resources <noreply@honeywellaerospace.com>",
     "Honeywell Aerospace Career Site - Please confirm your identity"),
    ("Other Employer <accounts@another-employer.example>", "Your one-time passcode"),
])
def test_ats_sender_hint_falls_back_to_recipient(mail_db, monkeypatch, caplog, sender, subject):
    now = time.time()
    queries = _mock_gmail(monkeypatch, [_message("fresh", now, sender=sender, subject=subject)],
                          strict_empty=True)
    with caplog.at_level(logging.INFO, logger="agent.mail"):
        result = mail.wait_for_verification(now - 5, sender_hint="oracle", to_alias="jane.doe@gmail.com",
                                            timeout=1, want="code")

    assert result and result.message_id == "fresh" and result.code == "638241"
    assert "from:oracle" in queries[0] and "from:" not in queries[1]
    assert "to:jane.doe@gmail.com" in queries[1]
    assert db.mail_is_seen("fresh")
    assert "recipient_fallback=True" in caplog.text
    assert "638241" not in caplog.text and "jane.doe@gmail.com" not in caplog.text


def test_sender_filter_is_not_removed_without_recipient(mail_db, monkeypatch):
    now = time.time()
    queries = _mock_gmail(monkeypatch, [_message("unrelated", now)], strict_empty=True)

    assert mail.wait_for_verification(now - 5, sender_hint="oracle", timeout=0.02, want="code") is None
    assert queries and all("from:oracle" in q for q in queries)
    assert not db.mail_is_seen("unrelated")


def test_thread_uses_each_message_internal_date(mail_db, monkeypatch):
    now = time.time()
    messages = [_message("old-0324", now - 6688), _message("fresh-0515", now),
                _message("old-0352", now - 4981)]
    _mock_gmail(monkeypatch, messages, strict_empty=True)

    result = mail.wait_for_verification(now - 10, sender_hint="oracle", to_alias="jane.doe@gmail.com",
                                        timeout=1, want="code")

    assert result and result.message_id == "fresh-0515"
    assert not db.mail_is_seen("old-0324") and not db.mail_is_seen("old-0352")


@pytest.mark.parametrize("newest_consumed", [False, True])
@pytest.mark.parametrize("newest_sender", ["careers@notifications.employer.example", "support@employer.example"])
def test_older_code_in_thread_is_not_reused(mail_db, monkeypatch, newest_consumed, newest_sender):
    now = time.time()
    newest = _message("newest", now, sender=newest_sender, body="No new verification code." if not newest_consumed
                      else "Your verification code is 915372.")
    _mock_gmail(monkeypatch, [_message("superseded", now - 10), newest])
    if newest_consumed:
        db.mark_mail_seen("newest")

    assert mail.wait_for_verification(now - 20, to_alias="jane.doe@gmail.com", timeout=0.02,
                                      want="code") is None
    assert not db.mail_is_seen("superseded")


def test_pre_request_message_is_rejected_within_same_second(mail_db, monkeypatch, caplog):
    request_ts = int(time.time()) - 2 + 0.7
    _mock_gmail(monkeypatch, [_message("stale", request_ts - 0.1)])
    with caplog.at_level(logging.INFO, logger="agent.mail"):
        result = mail.wait_for_verification(request_ts, to_alias="jane.doe@gmail.com", timeout=0.02,
                                            want="code")

    assert result is None and not db.mail_is_seen("stale")
    assert "'stale':" in caplog.text


def test_declared_expired_code_is_not_returned_or_consumed(mail_db, monkeypatch, caplog):
    now = time.time()
    _mock_gmail(monkeypatch, [_message("expired", now - 601,
                body="Your one-time pass code: 638241. This code will expire in 10 minutes.")])
    with caplog.at_level(logging.INFO, logger="agent.mail"):
        result = mail.wait_for_verification(now - 700, to_alias="jane.doe@gmail.com", timeout=0.02,
                                            want="code")

    assert result is None and not db.mail_is_seen("expired")
    assert "'expired':" in caplog.text and "638241" not in caplog.text


@pytest.mark.parametrize("expected,actual,delivered", [
    ("jane.doe+adt@gmail.com", "jane.doe+other@gmail.com", "jane.doe@gmail.com"),
    ("jane.doe@gmail.com", "jane.doe+other@gmail.com", "jane.doe@gmail.com"),
    ("jane.doe+adt@gmail.com", "jane.doe@gmail.com", "jane.doe@gmail.com"),
    ("jane.doe@gmail.com", "someone.else@gmail.com", "someone.else@gmail.com"),
])
def test_recipient_fallback_preserves_mailbox_and_plus_alias(mail_db, monkeypatch, expected, actual, delivered):
    now = time.time()
    _mock_gmail(monkeypatch, [_message("wrong-recipient", now, recipient=actual, delivered_to=delivered)],
                strict_empty=True)

    assert mail.wait_for_verification(now - 5, sender_hint="oracle", to_alias=expected, timeout=0.02,
                                      want="code") is None
    assert not db.mail_is_seen("wrong-recipient")


def test_recipient_fallback_accepts_equivalent_gmail_spelling(mail_db, monkeypatch):
    now = time.time()
    queries = _mock_gmail(monkeypatch, [_message("equivalent", now, recipient="janedoe+adt@gmail.com")],
                          strict_empty=True)

    result = mail.wait_for_verification(now - 5, sender_hint="oracle", to_alias="jane.doe+adt@googlemail.com",
                                        timeout=1, want="code")

    assert result and result.message_id == "equivalent"
    assert "to:janedoe+adt@gmail.com" in queries[1]
    assert "to:janedoe@gmail.com" not in queries[1]


def test_recipient_fallback_checks_delivery_headers(mail_db, monkeypatch):
    now = time.time()
    queries = _mock_gmail(monkeypatch, [_message("delivered", now, recipient="undisclosed-recipients:;",
                                                delivered_to="jane.doe+adt@gmail.com")], strict_empty=True)

    result = mail.wait_for_verification(now - 5, sender_hint="oracle", to_alias="jane.doe+adt@gmail.com",
                                        timeout=1, want="code")

    assert result and result.message_id == "delivered"
    assert "deliveredto:jane.doe+adt@gmail.com" in queries[1]


def test_ambiguous_fallback_does_not_choose_another_employer(mail_db, monkeypatch, caplog):
    now = time.time()
    messages = [_message("employer-a", now), _message("employer-b", now - 1,
                sender="Another Employer <noreply@another.example>", thread="other-thread")]
    _mock_gmail(monkeypatch, messages, strict_empty=True)
    with caplog.at_level(logging.INFO, logger="agent.mail"):
        result = mail.wait_for_verification(now - 5, sender_hint="oracle", to_alias="jane.doe@gmail.com",
                                            timeout=0.02, want="code")

    assert result is None and "'ambiguous':" in caplog.text
    assert not db.mail_is_seen("employer-a") and not db.mail_is_seen("employer-b")


def test_recipient_fallback_rejects_unrelated_discount_code(mail_db, monkeypatch):
    now = time.time()
    _mock_gmail(monkeypatch, [_message("promotion", now, subject="Your discount code",
                                      body="Your discount code is 638241.")], strict_empty=True)

    assert mail.wait_for_verification(now - 5, sender_hint="oracle", to_alias="jane.doe@gmail.com",
                                      timeout=0.02, want="code") is None
    assert not db.mail_is_seen("promotion")


def test_gmail_paginates_and_reads_nested_html_body(mail_db, monkeypatch):
    now = time.time()
    old = [_message(f"old-{i}", now - 60) for i in range(10)]
    fresh = _message("page-two", now)
    payload = fresh["payload"]
    fresh["payload"] = {"mimeType": "multipart/mixed", "headers": payload["headers"], "parts": [
        {"mimeType": "multipart/alternative", "parts": [payload]}]}
    messages = old + [fresh]
    pages, reads = [], []

    def fake_gapi(path, **params):
        if path == "messages":
            pages.append(params.get("pageToken"))
            if not params.get("pageToken"):
                return {"messages": [{"id": m["id"]} for m in old], "nextPageToken": "page-2"}
            return {"messages": [{"id": old[0]["id"]}, {"id": "page-two"}]}
        message_id = path.split("/", 1)[1]
        reads.append(message_id)
        return next(m for m in messages if m["id"] == message_id)

    monkeypatch.setattr(mail, "_gapi", fake_gapi)
    result = mail.wait_for_verification(now - 5, to_alias="jane.doe@gmail.com", timeout=1, want="code")

    assert result and result.message_id == "page-two" and result.code == "638241"
    assert pages == [None, "page-2"]
    assert len(reads) == 11 and len(set(reads)) == 11
