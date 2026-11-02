# Snapshot v0.10.1 — 2026-11-02 11:00 (+08)

Copy pass: everything user-facing, and all four documents, are English now.
One behaviour fix came along with it.

Previous snapshot: `SNAPSHOT-v0.10.0.md` (architecture). `SNAPSHOT-v0.9.1.md`
is history and stays in Chinese.

## Changed

| Change | Why |
|---|---|
| **Chat replies work** | the model call used `tool_choice="required"`, so a turn that was spoken to had to answer with a tool call and returned no text — the page showed "…" forever. When `said` is present it now uses `tool_choice="auto"` and may answer with words |
| **The agent is framed as autonomous** | the prompt opened with "you never write prose" and "you only speak when spoken to", which read like a voice switch. It now opens with "Nobody operates you… decide on your own how to look", and a message is "one more thing you noticed, not a remote control" |
| **The lamp answers in your language** | the prompt, the page and every file are English; the lamp's replies follow the language the person typed (Chinese by default) |
| **`lamp.sh`** | one command for host + page: `start / stop / status / logs / say`. Logs go to `host/logs/`, closing the terminal no longer kills anything, `stop` also finds processes it did not start |
| **All copy in English** | web page labels, buttons, placeholders, chat fallbacks, CLI output, log messages, code comments, and the four documents |
| `logs/` in `.gitignore` | it is runtime output |

Documents were rewritten, not translated: they describe the current two-layer
system instead of carrying the v2.1 texts along. `README.md` 407 → 407 lines but
with the legacy sections gone; `FIRMWARE-API.md` 600 → 199 lines, now the current
contract rather than a design draft with migration tables; `PROTOCOL.md` 252
lines.

## Verified

- host: **76 tests pass**
- firmware: **70 checks pass**, `-Wall -Wextra` clean
- end-to-end dry run: 25 Hz stream, `G` ×1, `C,0,0,0,0` ×1, `F` continuous, `K`
  transients, `light` / `blink` / `say` / `observe` all answering
- `lamp.sh` start/status/stop exercised against a live host and page

## Still unverified

**The chat reply has not been confirmed on the running lamp.** The fix is in and
unit-tested with a scripted client, but the real model has not been asked a
question since. Restart the host and say something; if it stays silent, the
`mind ->` line in `host/logs/host.log` says whether the model produced words,
tools, or neither.

## Run

```bash
cd host
./lamp.sh start          # host + page, logs in host/logs/
./lamp.sh status
./lamp.sh say "a little darker"
./lamp.sh stop
```

## Next

1. Confirm the chat reply, then tag `v1.0.0`
2. Firmware v2.3: remove `M`/`LID`/`L`/`U`/`C`/`R` and the preset table (reflash)
3. BlackHole, so the beat is measured rather than guessed
4. VL53L0X for a real distance
