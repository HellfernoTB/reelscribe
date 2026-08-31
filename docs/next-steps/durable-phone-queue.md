# Next step: durable phone-side queue for reel ingestion

Status: **planned, not started.**

## Context

Today the phone shares a reel URL via a plain HTTP GET to `reelscribe serve` on
the box (over Tailscale). If that request fails for any reason — `serve`
restarting, box rebooting, Tailscale hiccup, phone briefly offline — the URL is
lost. README says so explicitly ("an offline box means that share is lost —
nothing is queued on the phone").

The box runs 24/7, so "box fully off" is not the real risk — transient
unreachability is. The fix: the phone keeps its own durable outbox and re-sends
until the box acknowledges. Two supporting box-side changes come along:
`Restart=always` on the service, and making a re-shared *failed* reel actually
retry (currently it returns `{"skipped"}` and never re-processes — a gap already
noted in `docs/doc-technique.md` §10).

Out of scope (considered, not chosen): systemd socket activation (only helps a
narrow crash window, fiddly with `ThreadingHTTPServer`); a `reelscribe run`
safety-net timer (box is 24/7 + `Restart=always` makes it marginal — noted as a
one-liner in docs only); Wake-on-LAN (box doesn't sleep).

---

## Part 1 — Box-side (in-repo, tested)

### 1a. `Restart=always`

**File:** `scripts/reelscribe-serve.service`

```ini
[Unit]
Description=reelscribe HTTP ingestion endpoint
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
EnvironmentFile=%h/reelscribe/serve.env
ExecStart=%h/.local/bin/reelscribe serve
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
```

`Restart=on-failure` → `always`; `RestartSec` 5 → 15; add
`StartLimitIntervalSec=0` so a persistently-misconfigured unit keeps retrying
instead of being latched off. Update the install-comment header's example too.
No code change. Verified manually (kill the process, confirm it comes back).

### 1b. Re-arm a `failed` item on re-share

**`src/reelscribe/state.py`** — new method (do **not** change `add()`; its
"None = already present" contract is used by `cmd_add` and existing tests):

```python
def reactivate(self, url: str) -> bool:
    """A previously FAILED link -> back to pending, error cleared.
    done/pending are left alone. attempts is preserved. Returns True if changed."""
    entry = self.items.get(normalize_url(url))
    if entry and entry["status"] == "failed":
        entry["status"] = "pending"
        entry.pop("error", None)
        return True
    return False
```

**`src/reelscribe/serve.py`** — `Ingest.enqueue()`, keep the `(normalized,
is_new)` 2-tuple return (every `serve.handle` test + the `enqueue_spy` fixture
depend on it). A re-armed item counts as `is_new` → `202 {"queued"}` + worker
wake:

```python
def enqueue(self, url: str) -> tuple[str, bool]:
    with self._state_lock:
        key = self._state.add(url)
        rearmed = self._state.reactivate(url) if key is None else False
        self._state.save()
    is_new = key is not None or rearmed
    if is_new:
        with self._cv:
            self._wakes += 1
            self._cv.notify_all()
    return normalize_url(url), is_new
```

`serve.handle()` unchanged (`is_new` already → `202 {"queued": normalized}`).

**`src/reelscribe/cli.py`** — `cmd_add()`: when `key is None`, call
`state.reactivate(url)`; print `re-queued (was failed): <key>` if True, else the
existing `already registered:` line.

**Tests to add:**
- `tests/test_state.py`: `reactivate` on failed → True + status/`error`/`attempts`
  assertions; on pending/done → False, unchanged; on unknown url → False.
- `tests/test_serve.py`: pre-seed `tmp_path/state.json` (via `State`) with one
  `failed` item, `Ingest(cfg, run_once=spy)`, `start()`, `enqueue(same_url)`,
  `wait_idle()` → `is_new is True` and `run_once` ran. Second test: pre-seeded
  `done` item → `is_new is False`.
- `tests/test_cli.py`: `_wire` a URL that fails, `add`+`run`, then
  `cli.main(["add", same_url])` → state back to `pending`, stdout has `re-queued`.

**Docs touched by 1b:** `docs/doc-technique.md` §10 — replace the "Limite connue"
paragraph with the `reactivate` semantics; routes table row "URL déjà connue" →
"déjà connue **et non-échouée**". README endpoint table + a new caveat line
("re-sharing a failed reel now re-queues it").

---

## Part 2 — Phone outbox (README + doc-technique, no repo code)

App: **HTTP Shortcuts** (`ch.rmy.android.http_shortcuts` v4.x). One persistent
variable + two shortcuts.

> The JS API details below (`sendHTTPRequest` response shape, throw-triggers-retry,
> `setVariable` from the after-block) are from general knowledge and must be
> smoke-tested on the device. The load-bearing offline mechanism —
> "Wait for connection / Retry on failure" turning a failed request into a
> Pending Execution — is documented app behaviour.

### Variable
- `outbox` — Text, "Remember last used value" ON, default `[]` (JSON array string;
  the app has no array type).

### Shortcut A — "Queue reel" (share target)
Trigger: **Show in share sheet**, accepts text/URL → `shared_url`. Makes **no
network call** (a dead box never even errors). "Run before execution":

```js
const raw = getVariable("outbox") || "[]";
let box; try { box = JSON.parse(raw); } catch (e) { box = []; }
const url = (shared_url || "").trim();
if (url && box.indexOf(url) === -1) {
    box.push(url);
    setVariable("outbox", JSON.stringify(box));
}
enqueueShortcut("Flush outbox");
abort();
```

### Shortcut B — "Flush outbox" (worker)
Not a share target. Settings: **Retry on failure = ON**, **Wait for connection =
ON**. **Main request:** `GET http://homelab:8765/health` (no token). This is the
trick — if the phone or box is unreachable, `/health` fails with a network error
→ HTTP Shortcuts parks it as a Pending Execution (WorkManager, survives reboot,
auto-retries on reconnect, ~1 day TTL). "Run after execution" (only reached once
`/health` returned 2xx):

```js
const raw = getVariable("outbox") || "[]";
let box; try { box = JSON.parse(raw); } catch (e) { box = []; }
if (box.length === 0) abort();

const base = "http://homelab:8765/add";
const token = "PASTE_TOKEN_HERE";
const kept = []; let sent = 0;

for (let i = 0; i < box.length; i++) {
    const url = box[i];
    let res;
    try {
        res = sendHTTPRequest(base + "?token=" + encodeURIComponent(token) +
                              "&url=" + encodeURIComponent(url), { method: "GET" });
    } catch (e) { kept.push(url); continue; }
    const code = res.status;
    if (code >= 200 && code < 300) sent++;          // 202 queued / 200 skipped
    else if (code === 400 || code === 401) sent++;  // permanent -> drop, don't wedge
    else kept.push(url);                            // 5xx / unexpected -> keep
}
setVariable("outbox", JSON.stringify(kept));
showToast(sent + " sent, " + kept.length + " waiting");
if (kept.length > 0) throw new Error("outbox not drained");  // re-arm retry
abort();
```

### Self-heal paths
- **Next share** — A always enqueues B, B drains the whole array, so one good
  share after the box is back clears the backlog.
- **Reconnect** — the parked `/health` Pending Execution retries automatically.
- **Optional Wi-Fi trigger** (Tasker/MacroDroid): "Wi-Fi connected = home SSID" →
  run "Flush outbox" via the HTTP Shortcuts plugin. Documented as optional.
- Fallback if `throw`-re-arms-retry proves false on-device: drop that line, rely
  on next-share + Wi-Fi trigger.

### Docs
- **README** "Automatic ingestion": replace the one-line Android instructions +
  step 3 with a "Durable phone outbox (Android)" subsection (variable + both
  shortcuts + the `/health` trick + self-heal list). Keep iOS as-is but note iOS
  Shortcuts has no durable-queue primitive.
- **README "Caveats":** delete "The box must be powered on … share is lost";
  replace with the outbox behaviour. Add one line: "(optional) a
  `reelscribe run` user timer can also drain `state.json` on a schedule if you
  don't trust `Restart=always` — skipped here since the box is always on."
- **doc-technique.md:** new "§11bis — Outbox téléphone (résilience hors-ligne)"
  with the same two-shortcut design; update §11 "Raccourci téléphone" to point to
  it; note `Restart=always` + `StartLimitIntervalSec=0` in §11.

---

## Verification

### Automated
```
python -m pytest -q
```
All new tests pass; every existing `serve.handle` / `enqueue_spy` /
`test_state` / `test_cli` test still passes unchanged.

### Manual — box
```sh
cp scripts/reelscribe-serve.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user restart reelscribe-serve

pkill -9 -f 'reelscribe serve'
sleep 20; systemctl --user is-active reelscribe-serve      # -> active
curl -s localhost:8765/health                              # -> {"status":"ok"}

# re-arm: pick a failed key from `reelscribe list --failed`
T=$(grep -oP 'REELSCRIBE_TOKEN=\K.*' ~/reelscribe/serve.env)
curl -s "localhost:8765/add?token=$T&url=<failed reel url>"  # -> {"queued": ...}
journalctl --user -u reelscribe-serve -f                     # worker re-processes it
```

### Manual — phone
1. Box up, share a reel → toast "1 sent, 0 waiting"; transcript on box.
2. `systemctl --user stop reelscribe-serve`; share 2 reels → toasts "0 sent, N
   waiting"; `outbox` variable is a 2-element JSON array.
3. Start `serve`, run "Flush outbox" (or share a 3rd reel) → "3 sent, 0 waiting";
   3 transcripts on box.
4. Airplane mode, share a reel, disable airplane mode → Pending Execution retries;
   reel arrives.
5. Wrong token in B → reel is dropped (401 branch), queue not wedged.

---

## Risks
- HTTP Shortcuts JS API unverified → user smoke-tests; `/health` mechanism relies
  only on documented behaviour; fallback noted.
- `outbox` JSON read-modify-write not atomic — two shares in the same second
  could race; low probability on a phone, fallback = newline-delimited var.
- `Restart=always` + repeated crash → restart loop; `RestartSec=15` +
  homelab context make it acceptable.
- Pending Execution ~1 day TTL vs a box down for days → reel still safe in
  `outbox`, heals on next share.
- `reactivate` deliberately ignores `done` items (no accidental re-transcription).
