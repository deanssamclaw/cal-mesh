# Running cal-mesh as services

Six processes. Three run all the time, three on a schedule. **Only the bridge touches the radio.**

| Process | Script | Runs | Needs |
|---|---|---|---|
| bridge | `bridge.py` | always (restart on crash) | `meshtastic` (requirements.txt) |
| responder | `responder.py` | always (restart on crash) | stdlib; `claude` CLI on `PATH` for model replies |
| dashboard | `dashboard.py` | always | stdlib; serves `127.0.0.1:8787` |
| learn | `learn.py --quiet` | daily 06:15 | stdlib |
| drafts | `drafts.py --limit 40` | daily 06:30 | stdlib |
| tracer | `tracer.py --enqueue` | every 2 h | stdlib |

## macOS (launchd) — what the reference install runs

`launchd/*.plist` are the six agents the reference install runs, with its paths replaced:
`__HOME__` → your home directory, `__PYTHON__` / `__MESH_PYTHON__` → a Python 3.12+ interpreter
(`__MESH_PYTHON__` must have `requirements.txt` installed; the simplest setup uses one venv for
both). Rename `com.example.*` if you like, then:

```bash
cp deploy/launchd/com.example.mesh-*.plist ~/Library/LaunchAgents/
sed -i '' "s#__HOME__#$HOME#g; s#__PYTHON__#$HOME/cal-mesh/.venv/bin/python#g; s#__MESH_PYTHON__#$HOME/cal-mesh/.venv/bin/python#g" ~/Library/LaunchAgents/com.example.mesh-*.plist
for f in ~/Library/LaunchAgents/com.example.mesh-*.plist; do launchctl bootstrap gui/$(id -u) "$f"; done
launchctl kickstart -k gui/$(id -u)/com.example.mesh-responder   # restart one after a code change
```

## Linux (systemd --user)

`systemd/` holds the same six as user units (three services, three service+timer pairs), all
pointing at `~/cal-mesh/.venv`:

```bash
python3 -m venv ~/cal-mesh/.venv && ~/cal-mesh/.venv/bin/pip install -r ~/cal-mesh/requirements.txt
mkdir -p ~/.config/systemd/user && cp ~/cal-mesh/deploy/systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cal-mesh-bridge cal-mesh-responder cal-mesh-dashboard
systemctl --user enable --now cal-mesh-learn.timer cal-mesh-drafts.timer cal-mesh-tracer.timer
loginctl enable-linger "$USER"      # keep running without a login session
```

**Config is re-read live** by the responder and dashboard; the bridge reads it at start (see
[`docs/operations.md`](../docs/operations.md), *Config hot-reload*). A code change needs the
responder and dashboard restarted. `./mesh status` looks for the reference install's launchd
labels (`com.cal.mesh-*`); with other labels or on Linux, use `launchctl list | grep mesh` or
`systemctl --user status 'cal-mesh-*'`.

The routing scorer is a separate service on its own box: [`../tools/system-one/`](../tools/system-one).
