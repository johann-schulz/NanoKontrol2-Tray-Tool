# nanokontrol-daemon

Turn a Korg nanoKONTROL2 into a hardware volume mixer for PipeWire/PulseAudio on **Linux**.
Faders and knobs control per-application volumes or audio devices, the mute buttons mute
whatever their fader controls, and the transport buttons drive a media player over MPRIS.

The reason this exists: every other tool I tried ignored the one problem that actually
matters in daily use — **Spotify resets its own stream volume on every track change and
every seek**, which throws away whatever you set with the fader. This daemon handles that
without a polling loop, and idles at essentially zero CPU.

Note that this project is purely AI-generated. Anyway, it works flawlessly on my machine ;)

## Features

- Per-application volume control (Spotify, Firefox, Discord, anything with a PulseAudio stream)
- Device volume control: default output, default input, or any specific sink/source
- Mute buttons, always bound to the target of the fader in the same group
- Transport buttons (play/pause, previous, next) via `playerctl`, optionally locked to one
  player so a background YouTube tab can never steal them
- Configurable at runtime from a tray icon — no config file editing, no restart
- Levels and mute states survive a restart
- Corrects applications that reset their own stream volume, event driven rather than polled
- Reconnects on its own when PipeWire restarts

## Requirements

- Linux with PipeWire (via `pipewire-pulse`) or PulseAudio
- Python 3.9+
- A desktop that supports StatusNotifierItem/AppIndicator. On GNOME this needs the
  *AppIndicator and KStatusNotifierItem Support* extension, which Ubuntu ships and enables
  by default. The daemon also runs fine headless — it just can't be reconfigured while running.

## Installation

```bash
git clone https://github.com/<user>/nanokontrol-daemon.git
cd nanokontrol-daemon
./install.sh
```

The script installs the system and Python dependencies, copies the daemon to
`~/.local/bin`, and sets up a systemd user service. It asks before touching anything with
`sudo`, is safe to re-run, and never overwrites an existing configuration.

- `./install.sh --no-service` — install without enabling autostart
- `./install.sh --uninstall` — remove the service and the installed script (configuration is kept)

To try it before installing anything permanent, run it straight from the checkout:

```bash
python3 nanokontrol-daemon.py
```

### Manual installation

```bash
# System packages
sudo apt install playerctl python3-pip libasound2-dev pkg-config \
                 python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1

# Python packages
pip install -r requirements.txt --break-system-packages

# The script
install -Dm755 nanokontrol-daemon.py ~/.local/bin/nanokontrol-daemon.py
```

PyGObject is intentionally not in `requirements.txt`: building it from pip pulls in a large
toolchain, and the distribution package is the one that matches your installed GTK.

### Autostart via systemd

`~/.config/systemd/user/nanokontrol-daemon.service`:

```ini
[Unit]
Description=nanoKONTROL2 mixer + transport control
After=graphical-session.target pipewire-pulse.service
PartOf=graphical-session.target
Wants=pipewire-pulse.service

[Service]
ExecStart=/usr/bin/python3 %h/.local/bin/nanokontrol-daemon.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now nanokontrol-daemon.service
journalctl --user -u nanokontrol-daemon.service -f
```

The unit binds to `graphical-session.target` rather than `default.target` because the tray
icon needs a running session. If the nanoKONTROL2 is not plugged in at start, the daemon
exits with an error and systemd retries every three seconds until it appears.

## Usage

Click the tray icon for the menu:

- **Mute: \<target\>** — one checkbox per assigned fader, mirroring the mute state
- **Settings …** — the assignment window
- **Rescan devices** — re-read devices and streams after plugging something in
- **Quit**

In the settings window each of the 8 faders and 8 knobs gets a dropdown. Choose from:

| Option | What it controls |
| --- | --- |
| *unassigned* | Control does nothing |
| Master (default output) | Volume of the current default sink |
| Microphone (default input) | Volume of the current default source |
| Application: … | All streams of that application |
| Output: … | One specific output device |
| Input: … | One specific input device |

The mute button of a group always toggles whatever that group's **fader** controls. Knobs
have no mute button — the hardware only has eight.

The application list is assembled from currently playing streams, the sound server's
stream-restore database (everything ever seen), and the saved `known_apps` list. If an
application is missing from all three, add it by hand with **Add application …**; the name
must match the stream's `application.name` property exactly. To find it:

```bash
pactl list sink-inputs | grep -i application.name
```

At the bottom, **MPRIS player** restricts the transport buttons to one player. Leave it
empty to let `playerctl` pick whichever player it likes. List the running ones with:

```bash
playerctl -l
```

Defaults on first start: faders 1–3 control Spotify, Firefox and Discord, fader 8 is the
master output, knob 8 is the microphone, transport is locked to `spotify`.

### Configuration files

`~/.config/nanokontrol/config.json` — assignments, written by the settings window:

```json
{
  "assignments": {
    "fader0": "app:Spotify",
    "fader7": "default_sink",
    "knob7": "default_source"
  },
  "transport_player": "spotify",
  "known_apps": ["Discord", "Firefox", "Spotify"]
}
```

`~/.config/nanokontrol/state.json` — last level (MIDI value 0–127) and mute state per
application. Device volumes are deliberately **not** stored here; the sound server already
remembers those, and duplicating that state only creates conflicts.

Both files are written atomically and debounced 0.4 s after the last change, so dragging a
fader does not hammer the disk.

## Troubleshooting

**No tray icon on GNOME.** Check that the AppIndicator extension is enabled
(`gnome-extensions list --enabled | grep -i appindicator`). Without it the daemon still
works, it just has no UI.

**`no nanoKONTROL2 MIDI port found`.** The port name must contain `nanoKONTROL2`. Check with
`aconnect -i` or `python3 -c "import rtmidi; print(rtmidi.MidiIn().get_ports())"`. Note that
the daemon opens the port once at startup — replugging the device requires a restart
(`systemctl --user restart nanokontrol-daemon.service`).

**An application ignores its fader.** Its `application.name` probably differs from what you
entered, or it changes per stream. Check with `pactl list sink-inputs`. As a fallback the
daemon also matches against `application.process.binary`.

**Transport buttons do nothing.** `playerctl -l` must list the configured player name while
it is running. Flatpak and Snap builds sometimes register under a different bus name.

---

# Developer notes

## MIDI mapping

These are the factory-default CC numbers of the nanoKONTROL2 in CC mode. Only the ones the
daemon uses are verified against real hardware; the rest come from Korg's default scene and
are listed for anyone who wants to extend the mapping.

| Control | CC | Used |
| --- | --- | --- |
| Sliders, group 1–8 | 0–7 | ✔ |
| Knobs, group 1–8 | 16–23 | ✔ |
| Solo, group 1–8 | 32–39 | |
| Mute, group 1–8 | 48–55 | ✔ |
| Rec, group 1–8 | 64–71 | |
| Play | 41 | ✔ (play-pause) |
| Stop | 42 | |
| Rewind | 43 | ✔ (previous track) |
| Fast forward | 44 | ✔ (next track) |
| Record | 45 | |
| Cycle | 46 | |
| Track ◀ / ▶ | 58 / 59 | |
| Marker set / ◀ / ▶ | 60 / 61 / 62 | |

Two things worth knowing:

- The daemon maps **rewind/fast-forward** to previous/next track, not the dedicated track
  buttons. That was a deliberate ergonomic choice, not a mistake — the transport cluster sits
  where your thumb lands.
- Continuous controls send absolute values 0–127. Buttons send 127 on press and 0 on release,
  which is why every button handler checks `value == 127`. Without that check, everything
  fires twice.

The device sends nothing on startup and has no motorised faders, so after a restart the
physical fader positions do not match the restored levels. There is no soft-takeover: the
first movement of a fader jumps the volume to the fader position. Implementing pickup mode
would be a self-contained change in `set_target_volume`.

## Architecture

Four threads, one process:

| Thread | Job |
| --- | --- |
| main | GTK main loop, tray, settings window |
| rtmidi callback | Receives CC messages, applies them directly |
| pulse listener | Blocks in `event_listen()` on its own connection, pushes events to a queue |
| event worker | Drains that queue and reconciles stream state |
| save | Debounced writes of `state.json` and the stream-restore entries |

There are **two** PulseAudio connections. `pulsectl` is not reentrant: you cannot make API
calls from inside an event callback on the same connection. So one connection does nothing
but listen, its callback only does a `queue.put()`, and a second connection handles every
read and write. Everything touching that second connection is serialised by a single
`RLock`.

The tray never touches PulseAudio from a signal handler without that lock either, and
worker threads that need to refresh the menu go through `GLib.idle_add(tray.rebuild_menu)`
rather than calling GTK across threads.

## The Spotify problem

Spotify re-applies its internal volume to its PulseAudio stream on every track change and
every seek, wiping whatever the fader set. The obvious fix is a polling loop that keeps
re-applying the desired volume. That is what the first version of this script did, at 50 ms
intervals, and it was bad in two ways:

1. **CPU.** The loop called `sink_input_list()` twenty times a second. That is a full
   enumeration of every stream including proplist construction — not a cheap call. The MIDI
   callback did the same thing per message, so simply dragging a fader spiked usage to ~5 %.
   Idle sat around 1 %, which is absurd for a utility that does nothing most of the time.
2. **Audibly wrong.** Polling is late by definition. Up to 50 ms of full-volume audio got
   through on every track change before the correction landed.

The current version fixes both with three independent mechanisms:

**Event subscription instead of polling.** The listener subscribes to `sink_input`, `sink`,
`source` and `server` events. When Spotify resets itself, the server says so and the
correction lands in about a millisecond. Idle cost is a thread blocked in `poll()`, i.e.
nothing.

**An index cache instead of enumeration.** `tracked_streams` maps sink-input index →
(application, channel count) and is maintained purely from the event stream. A fader tick is
now a single `sink_input_volume_set(index, …)` call. `sink_input_list()` only runs on connect
and after a config change.

**Stream-restore, which removes the glitch entirely.** Every write also goes into the
server's stream-restore database (`sink-input-by-application-name:Spotify`). When Spotify
tears down and recreates its stream — which is what actually happens on a track change — the
server applies the stored volume at stream *creation*. The stream is never loud in the first
place, so there is nothing to correct after the fact. This is best-effort: if the extension
is unavailable, the daemon logs once, disables it, and falls back to event-driven correction
only.

### The feedback loop

Every volume write we perform comes back to us as a `change` event. Reacting to that with
another write would loop forever. So `enforce_stream()` reads the current value first and
only writes when it differs by more than `VOLUME_TOLERANCE` (0.005). The sequence settles
after exactly one extra read.

### Adopting unknown applications

When a stream appears for an application with no stored level, the daemon takes the stream's
*current* volume as the desired one instead of forcing a default. Otherwise a newly assigned
application would get yanked to an arbitrary value the first time it plays anything, before
its fader was ever touched. From that point on, normal enforcement applies.

### Device index invalidation

Sink and source indices are cached by name, but indices are not stable across device changes.
Any `sink` or `source` event clears the cache; the next access re-resolves via
`get_sink_by_name()`. Without this, unplugging and replugging an interface leaves the daemon
writing to a dead index.

## Known limitations

- Application matching is by `application.name` (falling back to
  `application.process.binary`), so two applications reporting the same name share a fader.
- No soft-takeover, see above.
- MIDI hot-plug is not handled; the port is opened once at startup.
- UI strings are hardcoded English, no gettext.
- Only tested on Ubuntu with PipeWire and GNOME. The PulseAudio API used is the compatibility
  layer, so real PulseAudio should work, but it is untested.

## License

MIT — see [LICENSE](LICENSE).
