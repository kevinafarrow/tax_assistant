from tax_assistant import gate

ALLOWED = ("kevin@example.com",)
TOKEN = "3f1c9a2e-1111-2222-3333-444455556666"
SPF_PASS = ("purelymail.com; spf=pass smtp.mailfrom=example.com; dkim=pass",)
SPF_FAIL = ("purelymail.com; spf=fail smtp.mailfrom=example.com",)


def check(**overrides):
    kwargs = dict(
        from_addr="kevin@example.com",
        auth_results_headers=SPF_PASS,
        subject="lunch receipt",
        body=f"client lunch {TOKEN}",
        allowed_senders=ALLOWED,
        bearer_token=TOKEN,
    )
    kwargs.update(overrides)
    return gate.check(**kwargs)


def test_all_factors_pass():
    assert check().ok


def test_sender_not_allowlisted():
    r = check(from_addr="attacker@evil.com")
    assert not r.ok and "allowlist" in r.reason


def test_sender_case_insensitive():
    assert check(from_addr="Kevin@Example.COM").ok


def test_spf_fail_rejected():
    r = check(auth_results_headers=SPF_FAIL)
    assert not r.ok and "SPF" in r.reason


def test_no_auth_results_rejected():
    assert not check(auth_results_headers=()).ok


def test_spf_softfail_not_accepted():
    r = check(auth_results_headers=("x; spf=softfail",))
    assert not r.ok


def test_missing_token_rejected():
    r = check(body="client lunch, no token here")
    assert not r.ok and "token" in r.reason


def test_token_in_subject_accepted():
    assert check(subject=f"receipt {TOKEN}", body="no token in body").ok


def test_empty_token_config_rejected():
    assert not check(bearer_token="").ok


def test_strip_token():
    assert TOKEN not in gate.strip_token(f"lunch {TOKEN} notes", TOKEN)
    assert gate.strip_token(None, TOKEN) == ""
