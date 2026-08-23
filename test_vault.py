"""Tests for the encrypted report vault.

The property that matters is NEGATIVE: the report must not be recoverable from
the file without the password. A test that only checks "right password works"
would pass just as happily on a hidden div.

Run:  python3 -m pytest test_vault.py -q
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import readiness_vault as V

SECRET = "V MART RETAIL LTD-HARYANA is short 1,746 dozen"
PAGE = f"<!doctype html><html><body><h1>{SECRET}</h1></body></html>"
PW = "correct horse battery staple"


def vault(page=PAGE, pw=PW):
    return V.lock(page, pw)


def payload(html):
    m = re.search(r"const V = (\{.*?\});", html, re.S)
    return json.loads(m.group(1))


def _open(p, pw):
    key = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(p["salt"]),
                              p["it"], dklen=32)
    return AESGCM(key).decrypt(base64.b64decode(p["iv"]),
                               base64.b64decode(p["ct"]),
                               base64.b64decode(p["salt"])).decode()


def test_the_report_is_not_in_the_file():
    """The whole point. No fragment of the report survives in the clear."""
    html = vault()
    assert SECRET not in html
    assert "V MART" not in html
    # NB: not "<h1> is absent" — the unlock card has its own heading. What
    # must be absent is the REPORT, so the check is on its content.
    assert "</body></html>" not in html.replace(
        html[html.rindex("</script>"):], "")
    for word in SECRET.split():
        if len(word) > 4:
            assert word not in html, f"{word!r} leaked into the vault"


def test_right_password_returns_it_exactly():
    assert _open(payload(vault()), PW) == PAGE


def test_wrong_password_fails_closed():
    with pytest.raises(InvalidTag):
        _open(payload(vault()), PW + "x")


def test_tampering_is_detected():
    """GCM authenticates: a doctored file must refuse to open, not open wrong."""
    p = payload(vault())
    ct = bytearray(base64.b64decode(p["ct"]))
    ct[10] ^= 1
    p["ct"] = base64.b64encode(bytes(ct)).decode()
    with pytest.raises(InvalidTag):
        _open(p, PW)


def test_salt_is_authenticated():
    """A ciphertext cannot be replayed under a different salt."""
    p = payload(vault())
    p["salt"] = base64.b64encode(b"x" * V.SALT_BYTES).decode()
    with pytest.raises(InvalidTag):
        _open(p, PW)


def test_every_lock_is_unique():
    """Fresh salt and nonce each time — two locks of one report must differ."""
    a, b = payload(vault()), payload(vault())
    assert a["salt"] != b["salt"]
    assert a["iv"] != b["iv"]
    assert a["ct"] != b["ct"]


def test_iterations_are_not_quietly_weakened():
    assert V.ITERATIONS >= 300_000
    assert payload(vault())["it"] == V.ITERATIONS


def test_empty_password_is_refused():
    with pytest.raises(ValueError):
        V.lock(PAGE, "")


def test_vault_is_self_contained():
    html = vault()
    for bad in ("<script src", "<link ", "@import", "http://", "https://"):
        assert bad not in html
