#!/usr/bin/env python3
"""Wrap a rendered report in a password-locked, genuinely encrypted page.

    from readiness_vault import lock
    vault_html = lock(report_html, password)

The point is that the report is NOT in the file. A password prompt that hides
a div is worthless — the data sits in the source and View Source defeats it.
Here the whole report is encrypted with AES-256-GCM under a key derived from
the password by PBKDF2-SHA256, and the file holds only ciphertext, the salt,
the nonce and the iteration count. Without the password there is nothing to
read, and GCM's tag means a tampered file fails to open rather than opening
wrong.

The reader needs no server, no network and no install: WebCrypto does the same
derivation in the browser. It works from a USB stick or an email attachment.

What this does NOT defend against: someone who knows the password, and someone
watching the screen after it is open. It is protection for a file in transit
and at rest, which is exactly the risk of forwarding a report that names every
party and what they are short of.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# PBKDF2 rounds. High enough to make guessing expensive, low enough that the
# browser derives the key in well under a second on the machines here. The
# value is stored in the file, so an old report still opens after this changes.
ITERATIONS = 310_000
SALT_BYTES = 16
NONCE_BYTES = 12


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def lock(page_html: str, password: str, label: str = "") -> str:
    """Encrypt `page_html` under `password` and return the unlock page."""
    if not password:
        raise ValueError("A vault with no password is just a slower file.")

    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                              ITERATIONS, dklen=32)
    # The salt is authenticated as associated data, so a ciphertext cannot be
    # replayed under a different salt to make a wrong password appear right.
    blob = AESGCM(key).encrypt(nonce, page_html.encode("utf-8"), salt)

    payload = json.dumps({
        "v": 1,
        "kdf": "PBKDF2-SHA256",
        "it": ITERATIONS,
        "salt": _b64(salt),
        "iv": _b64(nonce),
        "ct": _b64(blob),
    }, separators=(",", ":"))

    return _SHELL.replace("__LABEL__", label or "Dispatch readiness report") \
                 .replace("/*__VAULT__*/null", payload)


# The unlock page. No network, no external anything — same rule as the report
# it carries. Deliberately plain: it is a door, not a document.
_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Locked — __LABEL__</title>
<style>
 :root{color-scheme:light dark}
 *{box-sizing:border-box}
 body{margin:0;height:100vh;display:grid;place-items:center;
      font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      background:#f6f6f5;color:#1a1a19}
 @media (prefers-color-scheme:dark){body{background:#151514;color:#eceae5}}
 .card{width:min(92vw,380px);padding:26px 24px;border-radius:10px;
       background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.12),0 8px 28px rgba(0,0,0,.08)}
 @media (prefers-color-scheme:dark){.card{background:#232320;box-shadow:none;
       border:1px solid #35342f}}
 h1{margin:0 0 4px;font-size:16px;font-weight:650}
 p{margin:0 0 18px;font-size:12.5px;color:#6b6a64}
 @media (prefers-color-scheme:dark){p{color:#9a978d}}
 input,button{width:100%;font:inherit;padding:9px 11px;border-radius:6px;
              border:1px solid #cfceC8;background:#fff;color:inherit}
 @media (prefers-color-scheme:dark){input,button{background:#1b1b19;
              border-color:#3d3c36}}
 input:focus{outline:2px solid #7a6a3a;outline-offset:1px}
 button{margin-top:9px;border:0;background:#33322c;color:#fff;font-weight:600;
        cursor:pointer}
 button:disabled{opacity:.55;cursor:default}
 .msg{margin-top:11px;font-size:12.5px;min-height:1.3em;color:#9a3b2f}
 .foot{margin-top:16px;font-size:11px;color:#8b8a83}
 iframe{position:fixed;inset:0;width:100%;height:100%;border:0}
</style></head><body>
<div class="card" id="card">
  <h1>__LABEL__</h1>
  <p>This file is encrypted. Enter the password to read it.</p>
  <form id="f" autocomplete="off">
    <input id="pw" type="password" placeholder="Password" autocomplete="off"
           autofocus aria-label="Password">
    <button id="go" type="submit">Unlock</button>
  </form>
  <div class="msg" id="msg" role="status"></div>
  <div class="foot">AES-256-GCM. The report is not in this file in readable
    form — there is nothing to view-source.</div>
</div>
<script>
const V = /*__VAULT__*/null;
const $ = id => document.getElementById(id);
const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));

async function unlock(pw){
  const enc = new TextEncoder();
  const salt = b64(V.salt);
  const base = await crypto.subtle.importKey('raw', enc.encode(pw), 'PBKDF2',
                                             false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey(
    {name:'PBKDF2', salt, iterations:V.it, hash:'SHA-256'},
    base, {name:'AES-GCM', length:256}, false, ['decrypt']);
  // additionalData must match what the writer authenticated: the salt.
  const plain = await crypto.subtle.decrypt(
    {name:'AES-GCM', iv:b64(V.iv), additionalData:salt}, key, b64(V.ct));
  return new TextDecoder().decode(plain);
}

$('f').addEventListener('submit', async e => {
  e.preventDefault();
  const pw = $('pw').value;
  if(!pw) return;
  $('go').disabled = true;
  $('msg').textContent = 'Unlocking…';
  try {
    const html = await unlock(pw);
    // The report keeps its own stylesheet and class names, so it goes into a
    // frame rather than over this page. srcdoc is set as a PROPERTY, so a
    // 400 KB report needs no attribute escaping.
    document.body.innerHTML = '';
    const f = document.createElement('iframe');
    f.setAttribute('sandbox', 'allow-scripts allow-popups allow-modals');
    document.body.appendChild(f);
    f.srcdoc = html;
  } catch (err) {
    // GCM cannot tell a wrong password from a damaged file, and neither can
    // we — so the message says both rather than guessing.
    $('msg').textContent = 'Wrong password, or this file has been altered.';
    $('go').disabled = false;
    $('pw').select();
  }
});
</script></body></html>
"""


def main():
    import argparse
    import getpass
    from pathlib import Path

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("page", type=Path, help="the rendered report HTML")
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--label", default="")
    a = ap.parse_args()

    # Never a --password flag: it would sit in the shell history and in `ps`.
    pw = os.environ.get("SNJ_REPORT_PASSWORD") or getpass.getpass("Password: ")
    if not pw:
        raise SystemExit("No password given.")
    if not os.environ.get("SNJ_REPORT_PASSWORD"):
        if getpass.getpass("Again: ") != pw:
            raise SystemExit("The two passwords differ.")

    a.out.write_text(lock(a.page.read_text(), pw, a.label))
    print(f"locked {a.page} -> {a.out}")


if __name__ == "__main__":
    main()
