# nanokontrol-daemon

Turn a Korg nanoKONTROL2 into a hardware volume mixer for PipeWire/PulseAudio on **Linux** —
and, with a separate port in [`windows/`](windows), for **Windows 11**.
Faders and knobs control per-application volumes or audio devices, the mute buttons mute
whatever their fader controls, and the transport buttons drive a media player over MPRIS
(on Windows: the Windows media controls).

Windows users: everything you need is in the [Windows](#windows) section; the rest of this
README up to it describes the Linux version.

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
- Optional speaker keep-alive: a short, inaudible burst at a fixed interval keeps active
  speakers from falling into standby (off by default)

## Requirements

For the Windows version see [Windows](#windows).

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
                 python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
                 libportaudio2

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
- **Speaker keep-alive** — switches the [keep-alive](#speaker-keep-alive) on and off
- **Settings …** — the assignment window
- **Keep-alive settings …** — the keep-alive window
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

### Speaker keep-alive

Some active speakers go into standby during quiet passages and wake up again with a crackle.
The keep-alive plays a short burst — by default inaudible — on the default output device at a
fixed interval, whether or not something else is playing, so they never get there. It is
off by default and configured only in its own window (**Keep-alive settings …**):

| Setting | Default | |
| --- | --- | --- |
| Keep-alive active | off | also the checkbox in the tray menu |
| Interval | 10 min | from the start of one burst to the start of the next |
| Burst length | 2 s | |
| Frequency | 20000 Hz | must be below half the sample rate |
| Level | −30 dBFS | 0 dBFS = full scale |
| Sample rate | 48000 Hz | 44100, 48000, 88200 or 96000 |
| Fade in/out | 0.05 s | avoids clicks |
| Pause while the PC is locked | off | |

**Test now** plays one burst with the values as they are in the window, without saving them
— set e.g. 100 Hz at −40 dBFS to actually hear it. **Reset to defaults** fills in the
defaults; nothing changes until **Save**. Switching the keep-alive on, or unlocking the PC
with *Pause while the PC is locked*, plays the first burst right away.

The burst goes through the normal output path, so the volume and mute of the output device
apply to it as well: with the master muted, nothing reaches the speakers.

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
  "known_apps": ["Discord", "Firefox", "Spotify"],
  "keepalive": {
    "enabled": false,
    "interval_minutes": 10.0,
    "duration_s": 2.0,
    "frequency_hz": 20000.0,
    "level_dbfs": -30.0,
    "sample_rate": 48000,
    "fade_s": 0.05,
    "pause_when_locked": false
  }
}
```

`keepalive` is written by the keep-alive window. Missing or invalid values fall back to the
defaults, numbers are clamped to the ranges the window allows.

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

**Keep-alive status says "error".** The burst is played through PortAudio
(`libportaudio2`); the message after "error:" is PortAudio's. *Pause while the PC is
locked* needs a desktop that reports the lock to logind (GNOME and KDE do); if it does not,
the journal says so at startup and the keep-alive simply keeps running.

---

# Windows

`windows/nanokontrol-windows.py` is a port of the daemon to Windows 11 with the same
controls, the same tray-driven configuration and the same config format. It is built to run
unattended for months: unplugging and replugging the controller, standby, changing the
default device or restarting applications need no restart and no reconfiguration.

## Requirements

- Windows 11 (Windows 10 should work, but is untested)
- Python 3.11+ from [python.org](https://www.python.org/downloads/windows/). No compiler is
  needed — every dependency ships prebuilt wheels, and MIDI is read through the Windows MIDI
  API directly, so there is no `python-rtmidi` dependency.
- No other program may hold the nanoKONTROL2 MIDI port (quit e.g. NK2Tray and remove it from
  autostart).

## Installation

```powershell
git clone https://github.com/johann-schulz/NanoKontrol2-Tray-Tool.git
cd NanoKontrol2-Tray-Tool
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
```

The script installs the Python packages from `windows/requirements.txt` for the current
user, copies the script to `%LOCALAPPDATA%\nanokontrol`, creates an autostart shortcut
(started windowless via `pythonw.exe`) and starts the mixer. It is safe to re-run — a
running instance is stopped and replaced — and never touches an existing configuration.

- `install.ps1 -NoAutostart` — install without the autostart shortcut
- `install.ps1 -NoStart` — do not start it at the end
- `install.ps1 -Uninstall` — stop it, remove the shortcut and the installed script
  (configuration and Python packages are kept)

To try it without installing, run it from the checkout in a console:

```powershell
pip install -r windows\requirements.txt
python windows\nanokontrol-windows.py --no-supervisor
```

`--midi-test` only logs the incoming control changes, `--debug` logs verbosely.

## Usage

Left-click the tray icon for the settings window, right-click for the menu:

- **Mute: \<target\>** — one checkbox per assigned fader, mirroring the mute state
- **Speaker keep-alive** — switches the keep-alive on and off
- **Settings …** — the assignment window, the same as on Linux
- **Keep-alive settings …** — the keep-alive window, the same as on Linux, see
  [Speaker keep-alive](#speaker-keep-alive)
- **Rescan / reconnect devices** — forces a fresh look at MIDI and audio; normally not needed
- **Open config folder** — config, state and log files
- **Quit**

The icon turns grey with a red dot while the controller is not connected or the audio system
is unavailable; the tooltip and the top of the menu say which.

Differences to the Linux version:

| | Linux | Windows |
| --- | --- | --- |
| Application names | `application.name` (`Spotify`) | process name as in Task Manager › Details (`Spotify.exe`) |
| Devices | sink/source names | endpoint ids, plus the remembered device name |
| Transport | MPRIS via `playerctl` | Windows media controls |
| Player lock | MPRIS name (`spotify`) | part of the player's app id (`Spotify`), case-insensitive |
| Config | `~/.config/nanokontrol/` | `%APPDATA%\nanokontrol\` |
| Autostart | systemd user service | shortcut in the Startup folder + built-in supervisor |
| Keep-alive lock detection | logind `LockedHint` | Windows session notifications |

Application names match case-insensitively and with or without `.exe`, so a config copied
from Linux keeps working. If the player lock is empty, the transport buttons go to whatever
Windows considers the current player; if nothing is registered there, plain media keys are
sent. If it is set and that player is not running, the buttons do nothing — just like on
Linux.

Defaults on first start: faders 1–3 control `Spotify.exe`, `firefox.exe` and `Discord.exe`,
fader 8 is the default output, knob 8 is the default input, transport is locked to Spotify.

### Configuration files

`%APPDATA%\nanokontrol\config.json` has the same layout as on Linux, plus:

```json
{
  "device_labels": {"{0.0.0.00000000}.{…}": "Headset (USB Audio)"},
  "midi_port": "nanoKONTROL2",
  "midi_idle_reopen_minutes": 15
}
```

- `device_labels` — names of assigned devices, written by the settings window, so an
  unplugged device is still shown by name and is found again if Windows gives it a new id
- `midi_port` — substring of the MIDI input port name
- `midi_idle_reopen_minutes` — re-open an idle MIDI port this often as a last line of
  defence against a silently dead handle; `0` disables it

`state.json` is the same as on Linux. Both files are written atomically, and the previous
valid version is kept as `.bak`. A damaged file is never silently replaced by defaults: it is
copied aside as `.broken-<timestamp>` and the backup is loaded instead. Assignments are never
dropped because an application is not running or a device is unplugged — the settings
window lists them as *currently unavailable* and they work again once it is back.

`nanokontrol.log` (service) and `supervisor.log` live in the same folder, rotated at 1 MB.

## Troubleshooting

**Icon stays grey, tooltip says the port is in use.** Another program holds the MIDI port —
usually a previous mixer tool. Quit it; the port is picked up within a second.

**Icon stays grey, controller not found.** Check the port name in Device Manager or with
`--midi-test`. If it does not contain `nanoKONTROL2`, set `midi_port` in `config.json`.

**An application ignores its fader.** The name must match its process name. Start it, open
the settings window — running applications are marked *(playing)* — and pick it from there.

**Transport buttons control the wrong player or nothing.** Open the settings window while the
player runs; the *Media player* list shows the app ids Windows reports. Any part of the id
works, e.g. `Spotify` for both the desktop and the Store version.

**Keep-alive status says "error".** The message after "error:" is PortAudio's (shipped with
the `sounddevice` package). After changing the default output device the next burst
re-initialises PortAudio on its own.

**It does not start at all.** A message box points to the log. Missing packages are fixed by
running `install.ps1` again.

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

Six threads, one process:

| Thread | Job |
| --- | --- |
| main | GTK main loop, tray, settings windows, logind lock signal |
| rtmidi callback | Receives CC messages, applies them directly |
| pulse listener | Blocks in `event_listen()` on its own connection, pushes events to a queue |
| event worker | Drains that queue and reconciles stream state |
| save | Debounced writes of `state.json` and the stream-restore entries |
| keep-alive | Sleeps until the next burst or a settings change, then plays it |

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

## Windows port

The Windows version keeps the Linux design where Windows allows it and differs where it
does not:

| Thread | Job |
| --- | --- |
| main | Qt event loop, tray, settings windows, lock notifications |
| audio | Owns every Core Audio COM object; applies fader commands, reconciles once a second |
| midi | Owns the WinMM port; messages arrive in its own message queue (`CALLBACK_THREAD`) |
| media | asyncio loop for the Windows media controls |
| save | Debounced writes of `state.json` |
| watchdog | Detects standby (wall clock jumps) and hung threads |
| keepalive | Plays the speaker keep-alive bursts |

**Reconciling instead of event subscription.** Windows offers COM callbacks for new sessions
and volume changes, but they run on threads of the audio service and are a common source of
crashes in Python. The audio thread instead re-reads devices and sessions once a second and
corrects whatever deviates. That costs a few COM calls per second, and nothing is lost by it:
Windows itself remembers the volume of every application, so a newly created session already
starts at the level last set — the job stream-restore does on Linux. Fader moves are applied
immediately and coalesced, so dragging a fader is one write per target per batch.

**One owner per resource.** All COM objects live in the audio thread (multi-threaded
apartment), so nothing is marshalled and the Qt thread never touches them. The MIDI and UI
threads only put commands into a queue. Worker threads publish a plain snapshot (devices,
running applications, mute states) that the UI reads.

**MIDI without a driver callback.** The WinMM port is opened with `CALLBACK_THREAD`: input
arrives as messages in the MIDI thread's queue, so no driver thread ever calls into Python.
The port list is checked every second; any change (unplug, replug, another MIDI device
shifting the indices), a close reported by the driver or a resume from standby leads to a
clean re-open. While the port is absent or busy, opening is simply retried.

**Devices by id, then by name.** Endpoint ids are stable, but a device plugged into another
USB port can get a new one. If an assigned id is gone, the device is looked up by its
remembered name, as long as that is unambiguous. Invalidated interfaces are re-activated
once before the whole audio state is rebuilt.

**Layers of recovery.** Every worker runs in a supervised thread that is restarted when it
dies. A watchdog ends the process if one of them stops reporting in for 90 s, and the
default launch mode is a small supervisor process that restarts the service on any abnormal
exit, with back-off. A clean *Quit* ends both. Named mutexes keep a second instance from
fighting over the MIDI port. On exit, interpreter teardown is skipped (`os._exit`) after the
state is saved, which avoids the classic crashes of COM objects being released too late.

## Speaker keep-alive

The feature is the same code in both scripts (deliberately duplicated, so each version stays
a single file):

- **Nothing runs between bursts.** The tone is computed once per settings change, in plain
  Python (no numpy, ~10 ms). For a burst a PortAudio stream is opened, the tone written and
  the stream closed again; in between no stream is open and the thread waits on an event
  until the next burst is due or the settings change. While the keep-alive is off it waits
  without any timeout on Linux. On Windows it wakes every 30 s, only to report to the
  watchdog.
- **PortAudio stays in its thread.** `sounddevice` is imported and used only in the
  keep-alive thread, so it never meets the COM setup of the audio or UI thread on Windows.
  It plays on PortAudio's default device (Windows: the sound mapper, Linux: ALSA `default`,
  i.e. PipeWire/PulseAudio), which follows the system's default output. If a burst fails —
  typically because the device list PortAudio read at start is outdated — PortAudio is
  re-initialised once and the burst retried; a lasting error is shown as status and logged
  once, and the thread keeps going.
- **Lock detection is pushed, not polled.** Windows: a hidden window registered with
  `WTSRegisterSessionNotification` receives `WM_WTSSESSION_CHANGE`. Linux: the daemon reads
  the user's display session from logind (it runs as a user service, outside the session)
  and subscribes to `PropertiesChanged` of its `LockedHint`. Both need the tray's event
  loop; without it the option has no effect.

## Known limitations

- Application matching is by `application.name` (falling back to
  `application.process.binary`), so two applications reporting the same name share a fader.
- No soft-takeover, see above.
- Linux: MIDI hot-plug is not handled; the port is opened once at startup. (The Windows
  version re-acquires the port on its own.)
- Windows: applications are matched by process name, so all processes of the same name share
  a fader; Windows system sounds cannot be assigned.
- UI strings are hardcoded English, no gettext.
- Linux: only tested on Ubuntu with PipeWire and GNOME. The PulseAudio API used is the
  compatibility layer, so real PulseAudio should work, but it is untested.
- Windows: only tested on Windows 11.
- Keep-alive: only the default output device; the device's volume and mute apply to the
  burst. On Linux it runs only while the daemon runs, i.e. not while it waits for the
  nanoKONTROL2 to be plugged in.

## License

MIT — see [LICENSE](LICENSE).
