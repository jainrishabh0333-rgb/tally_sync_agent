# Tally Sync Agent

Runs on the machine where **TallyPrime** is running (including a hosted
"Tally on Cloud" Windows server). Reads Tally over its local XML gateway and
pushes the data to a Frappe site over HTTPS.

**It never writes to Tally.** Only Export requests are sent, so your books
cannot be modified by this program.

---

## Windows setup, step by step

### 1. Install Python

Download from <https://www.python.org/downloads/> and run the installer.

⚠️ On the first installer screen, tick **"Add python.exe to PATH"** at the
bottom before clicking Install. This is the single most common mistake.

No administrator rights? Choose **Customize installation** → **Install for me
only**. That works fine.

Check it worked — open **Command Prompt** and run:

```
python --version
```

You should see `Python 3.10` or higher. If you get *"not recognized"*, the
PATH box wasn't ticked — re-run the installer and choose **Modify**.

### 2. Get these files onto the server

On the Tally server, open GitHub in a browser, click the green **Code**
button → **Download ZIP**. Extract to a simple path such as:

```
C:\tally_bridge\
```

### 3. Install the dependencies

Open **Command Prompt**, then:

```
cd C:\tally_bridge
pip install -r requirements.txt
```

### 4. Test the Tally connection

This needs no Frappe account and changes nothing:

```
python test_tally.py
```

It prints the companies Tally has open, ledger counts, your largest debtor
balances and a sample of recent vouchers. **Compare those numbers against
Tally itself** — if they match, the connection is sound.

### 5. Configure Frappe

Copy `config.example.toml` to `config.toml` and fill in your Frappe site URL
and API key/secret. Then:

```
python sync.py --check
```

### 6. First full sync

```
python sync.py --full
```

### 7. Schedule it

In PowerShell **as Administrator**:

```
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1
```

Runs every 15 minutes, surviving reboots.

---

## Important for hosted Tally

The gateway only answers while **TallyPrime is open with a company loaded**.
On a hosted server, check with your provider whether Tally keeps running after
you disconnect your Remote Desktop session. If it closes on disconnect,
scheduled syncing will only work while someone is logged in.

Ask your provider:

> "Does our Tally session keep running when we disconnect from Remote Desktop,
> or does Tally close? Can a scheduled task run on the server unattended?"

---

## Files

| File | Purpose |
|---|---|
| `test_tally.py` | Read-only connection test. Start here. |
| `sync.py` | The agent itself. `--check`, `--full`, `--from/--to` |
| `tally_client.py` | TallyPrime XML reader |
| `frappe_client.py` | Pushes to Frappe |
| `install_windows.ps1` | Registers the scheduled task |
| `config.example.toml` | Copy to `config.toml` and edit |

## Troubleshooting

| Problem | Fix |
|---|---|
| `python` not recognized | Re-run installer → Modify → tick "Add to PATH" |
| `Could not reach Tally` | Tally closed, or not set to Server mode on port 9000 |
| No companies listed | Open your company in Tally |
| No ledgers returned | Company name in `config.toml` doesn't match exactly |
| `pip` SSL errors | Corporate proxy — ask IT for the proxy address |
