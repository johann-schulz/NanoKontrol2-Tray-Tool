#!/usr/bin/env python3
"""nanoKONTROL2 -> PulseAudio / PipeWire mixer + transport control.

Event driven, no polling: the sink-input events of the sound server drive the
volume corrections for apps that reset their own stream volume (Spotify does
this on every track change and every seek).

On top of that:
  * assignments for every fader and knob are configurable at runtime from a
    tray icon, no restart and no editing of this file needed
  * fader levels and mute states of applications survive a restart
  * mute buttons always follow the fader of the same group

Config lives in ~/.config/nanokontrol/{config,state}.json.
"""

import array
import json
import math
import os
import queue
import signal
import subprocess
import threading
import time

import pulsectl
import rtmidi

# The tray is optional: without a graphical session the daemon still runs, it
# just cannot be reconfigured while running.
TRAY_AVAILABLE = True
TRAY_IMPORT_ERROR = None
try:
    import gi
    gi.require_version('Gtk', '3.0')
    try:
        gi.require_version('AyatanaAppIndicator3', '0.1')
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        gi.require_version('AppIndicator3', '0.1')
        from gi.repository import AppIndicator3 as AppIndicator
    from gi.repository import Gtk, GLib, Gio
except (ImportError, ValueError) as err:
    TRAY_AVAILABLE = False
    TRAY_IMPORT_ERROR = err


# --- Hardware map (verified CC numbers for this nanoKONTROL2) ---
FADER_CCS = list(range(0, 8))     # Group1..8 / Slider
KNOB_CCS = list(range(16, 24))    # Group1..8 / Knob
MUTE_CCS = list(range(48, 56))    # Group1..8 / Mute -> follows the fader
TRANSPORT_CC = {
    41: "play-pause",
    43: "previous",
    44: "next",
}

CONTROL_ORDER = ([("fader%d" % i, cc) for i, cc in enumerate(FADER_CCS)] +
                 [("knob%d" % i, cc) for i, cc in enumerate(KNOB_CCS)])
CC_TO_CONTROL = {cc: control for control, cc in CONTROL_ORDER}
CONTROL_TO_CC = dict(CONTROL_ORDER)
# Mute button of a group toggles whatever that group's fader controls.
MUTE_CC_TO_CONTROL = {cc: "fader%d" % i for i, cc in enumerate(MUTE_CCS)}

# --- Tuning ---
# Volume difference below which a stream counts as "already correct". Needed
# to break the feedback loop: every write we do comes back as a change event.
VOLUME_TOLERANCE = 0.005

# Delay after the last change before state and stream-restore are written.
SAVE_DEBOUNCE_SECONDS = 0.4

TEST_MODE = False

# --- Speaker keep-alive ------------------------------------------------------
#
# Some active speakers fall into standby during quiet passages and wake up
# again with a crackle. A short, inaudible burst every few minutes keeps them
# awake. Between bursts no audio stream is open and the thread just sleeps;
# the tone itself is computed once per settings change.

KEEPALIVE_DEFAULTS = {
    "enabled": False,
    "interval_minutes": 10.0,     # time from the start of one burst to the next
    "duration_s": 2.0,            # length of one burst
    "frequency_hz": 20000.0,
    "level_dbfs": -30.0,          # 0 = full scale
    "sample_rate": 48000,
    "fade_s": 0.05,               # fade in/out against clicks
    "pause_when_locked": False,
}
KEEPALIVE_SAMPLE_RATES = (44100, 48000, 88200, 96000)
# key -> (minimum, maximum); frequency is further limited to below Nyquist
KEEPALIVE_LIMITS = {
    "interval_minutes": (0.5, 240.0),
    "duration_s": (0.1, 60.0),
    "frequency_hz": (10.0, 48000.0),
    "level_dbfs": (-90.0, 0.0),
    "fade_s": (0.0, 5.0),
}


def keepalive_problem(settings):
    """Human readable reason why the settings cannot work, or None."""
    nyquist = settings['sample_rate'] / 2
    if settings['frequency_hz'] >= nyquist:
        return ("The frequency must be below half the sample rate (%.0f Hz). "
                "Lower it or raise the sample rate." % nyquist)
    if settings['duration_s'] >= settings['interval_minutes'] * 60:
        return "The burst must be shorter than the interval."
    return None


def normalize_keepalive(raw):
    """Defaults for everything missing or invalid, numbers clamped to their
    limits. Never raises, whatever is in the file."""
    settings = dict(KEEPALIVE_DEFAULTS)
    if not isinstance(raw, dict):
        return settings
    for key in ('enabled', 'pause_when_locked'):
        if isinstance(raw.get(key), bool):
            settings[key] = raw[key]
    for key, (low, high) in KEEPALIVE_LIMITS.items():
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
            settings[key] = float(min(max(value, low), high))
    if raw.get('sample_rate') in KEEPALIVE_SAMPLE_RATES:
        settings['sample_rate'] = int(raw['sample_rate'])
    if keepalive_problem(settings):
        # Inconsistent combination (hand edited file): fall back to the
        # defaults for the dependent values rather than refusing to start.
        for key in ('frequency_hz', 'sample_rate', 'duration_s', 'interval_minutes'):
            settings[key] = KEEPALIVE_DEFAULTS[key]
    return settings


def build_keepalive_tone(settings):
    """Mono float32 sine with linear fade-in/out, as raw bytes."""
    rate = settings['sample_rate']
    count = int(settings['duration_s'] * rate)
    amplitude = 10 ** (settings['level_dbfs'] / 20.0)
    step = 2 * math.pi * settings['frequency_hz'] / rate
    samples = array.array('f', (amplitude * math.sin(step * i) for i in range(count)))
    # Fades never exceed half the tone length.
    fade = min(int(settings['fade_s'] * rate), count // 2)
    if fade > 1:
        for i in range(fade):
            gain = i / (fade - 1)
            samples[i] *= gain
            samples[count - 1 - i] *= gain
    return samples.tobytes()


# --- Paths ---
CONFIG_DIR = os.path.join(
    os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')),
    'nanokontrol')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.json')
STATE_PATH = os.path.join(CONFIG_DIR, 'state.json')

# Targets are stored as flat strings: "none", "app:<application.name>",
# "sink:<device name>", "source:<device name>", "default_sink",
# "default_source". Flat keeps config entries and combo box ids identical, so
# no translation layer is needed anywhere.
DEFAULT_CONFIG = {
    "assignments": {
        "fader0": "app:Spotify",
        "fader1": "app:Firefox",
        "fader2": "app:Discord",
        "fader7": "default_sink",
        "knob7": "default_source",
    },
    "transport_player": "spotify",
    "known_apps": ["Spotify", "Firefox", "Discord"],
    # speaker keep-alive, see KEEPALIVE_DEFAULTS
    "keepalive": dict(KEEPALIVE_DEFAULTS),
}

# --- State ---
state_lock = threading.RLock()

config = dict(DEFAULT_CONFIG)
assigned_apps = set()          # application names currently bound to a control

app_levels = {}                # application name -> MIDI value 0..127
app_mutes = {}                 # application name -> bool
touched_apps = set()           # apps with a deliberate user setting

tracked_streams = {}           # sink input index -> (app name, channel count)
device_cache = {}              # (kind, device name) -> (index, channel count)
device_labels = {}             # device name -> human readable description
default_sink = None            # (index, channel count)
default_source = None          # (index, channel count)

pulse_ctl = None               # connection used for every read/write
event_queue = queue.Queue()
save_pending = threading.Event()
stream_restore_supported = True
tray = None
keepalive = None


# --- Target helpers ----------------------------------------------------------

def target_kind(target):
    if not target or target == 'none':
        return 'none'
    return target.split(':', 1)[0]


def target_name(target):
    return target.split(':', 1)[1] if ':' in target else ''


def target_label(target):
    kind = target_kind(target)
    if kind == 'none':
        return "unassigned"
    if kind == 'app':
        return target_name(target)
    if kind == 'default_sink':
        return "Master (default output)"
    if kind == 'default_source':
        return "Microphone (default input)"
    name = target_name(target)
    return device_labels.get(name, name)


def refresh_assigned_apps():
    global assigned_apps
    assigned_apps = {target_name(t) for t in config['assignments'].values()
                     if target_kind(t) == 'app'}


# --- Persistence -------------------------------------------------------------

def atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def load_config():
    global config
    config = {
        'assignments': dict(DEFAULT_CONFIG['assignments']),
        'transport_player': DEFAULT_CONFIG['transport_player'],
        'known_apps': list(DEFAULT_CONFIG['known_apps']),
        'keepalive': dict(KEEPALIVE_DEFAULTS),
    }
    try:
        with open(CONFIG_PATH, encoding='utf-8') as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        refresh_assigned_apps()
        return
    except (OSError, ValueError) as err:
        print("config unreadable, using defaults:", err)
        refresh_assigned_apps()
        return
    # Only known control ids are taken over, so a hand edited file cannot
    # inject assignments for controls that do not exist.
    stored_assignments = stored.get('assignments') or {}
    config['assignments'] = {
        control: target for control, target in stored_assignments.items()
        if control in CONTROL_TO_CC and isinstance(target, str)}
    config['transport_player'] = stored.get('transport_player', '') or ''
    config['known_apps'] = [name for name in (stored.get('known_apps') or [])
                            if isinstance(name, str)]
    config['keepalive'] = normalize_keepalive(stored.get('keepalive'))
    refresh_assigned_apps()


def save_config():
    config['known_apps'] = sorted(set(config.get('known_apps') or []) | assigned_apps)
    atomic_write_json(CONFIG_PATH, {
        'assignments': config['assignments'],
        'transport_player': config.get('transport_player', ''),
        'known_apps': config['known_apps'],
        'keepalive': config['keepalive'],
    })


def load_state():
    try:
        with open(STATE_PATH, encoding='utf-8') as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as err:
        print("state unreadable, starting fresh:", err)
        return
    for name, value in (stored.get('levels') or {}).items():
        if isinstance(value, int) and 0 <= value <= 127:
            app_levels[name] = value
            # Anything restored from disk is a deliberate user setting, so it
            # takes part in the stream-restore writes right from the start.
            touched_apps.add(name)
    for name, value in (stored.get('mutes') or {}).items():
        if isinstance(value, bool):
            app_mutes[name] = value


def save_state():
    atomic_write_json(STATE_PATH, {'levels': app_levels, 'mutes': app_mutes})


# --- Applying state to the server -------------------------------------------

def _volume(value_flat, channels):
    return pulsectl.PulseVolumeInfo(value_flat, channels)


def match_app(sink_input):
    """Map a sink input to one of the assigned application names."""
    props = sink_input.proplist
    candidates = (
        props.get('application.name', ''),
        props.get('application.process.binary', ''),
    )
    for name in assigned_apps:
        for candidate in candidates:
            if candidate and candidate.lower() == name.lower():
                return name
    return None


def resolve_device(kind, name):
    """Look up a sink/source index by name, cached. The cache is dropped
    whenever the server reports device changes, so a re-plugged device gets a
    fresh index instead of a stale one."""
    key = (kind, name)
    if key in device_cache:
        return device_cache[key]
    try:
        if kind == 'sink':
            device = pulse_ctl.get_sink_by_name(name)
        else:
            device = pulse_ctl.get_source_by_name(name)
    except pulsectl.PulseError:
        return None
    entry = (device.index, len(device.volume.values))
    device_cache[key] = entry
    device_labels[name] = device.description or name
    return entry


def refresh_defaults():
    global default_sink, default_source
    info = pulse_ctl.server_info()
    sink = pulse_ctl.get_sink_by_name(info.default_sink_name)
    default_sink = (sink.index, len(sink.volume.values))
    source = pulse_ctl.get_source_by_name(info.default_source_name)
    default_source = (source.index, len(source.volume.values))


def adopt_unknown_app(name, info):
    """First time an app shows up with nothing stored for it: take its current
    volume as the desired one. Avoids yanking a freshly assigned app to some
    arbitrary default just because its fader was never touched."""
    if name not in app_levels:
        app_levels[name] = int(round(info.volume.value_flat * 127))
        app_mutes.setdefault(name, bool(info.mute))


def rescan_streams():
    """Full enumeration - only on connect or after a config change, never in
    the hot path."""
    tracked_streams.clear()
    for sink_input in pulse_ctl.sink_input_list():
        name = match_app(sink_input)
        if name:
            tracked_streams[sink_input.index] = (name, len(sink_input.volume.values))
            adopt_unknown_app(name, sink_input)
            enforce_stream(sink_input.index, sink_input)


def enforce_stream(index, info=None):
    """Push the stored level/mute onto one stream, but only where it actually
    deviates. This replaces the old polling loop: it runs when the server says
    the stream changed, i.e. right after Spotify resets itself."""
    entry = tracked_streams.get(index)
    if entry is None:
        return
    name, channels = entry
    if name not in assigned_apps or name not in app_levels:
        return
    if info is None:
        try:
            info = pulse_ctl.sink_input_info(index)
        except pulsectl.PulseIndexError:
            tracked_streams.pop(index, None)
            return
    target = app_levels[name] / 127.0
    if abs(info.volume.value_flat - target) > VOLUME_TOLERANCE:
        pulse_ctl.sink_input_volume_set(index, _volume(target, channels))
    wanted_mute = app_mutes.get(name, False)
    if bool(info.mute) != wanted_mute:
        pulse_ctl.sink_input_mute(index, wanted_mute)


def set_target_volume(target, midi_value):
    kind = target_kind(target)
    level = midi_value / 127.0
    if kind == 'app':
        name = target_name(target)
        app_levels[name] = midi_value
        touched_apps.add(name)
        for index, (stream_name, channels) in list(tracked_streams.items()):
            if stream_name == name:
                pulse_ctl.sink_input_volume_set(index, _volume(level, channels))
        save_pending.set()
    elif kind == 'default_sink':
        if default_sink:
            pulse_ctl.sink_volume_set(default_sink[0], _volume(level, default_sink[1]))
    elif kind == 'default_source':
        if default_source:
            pulse_ctl.source_volume_set(default_source[0], _volume(level, default_source[1]))
    elif kind in ('sink', 'source'):
        device = resolve_device(kind, target_name(target))
        if device is None:
            return
        index, channels = device
        if kind == 'sink':
            pulse_ctl.sink_volume_set(index, _volume(level, channels))
        else:
            pulse_ctl.source_volume_set(index, _volume(level, channels))


def toggle_target_mute(target):
    kind = target_kind(target)
    if kind == 'app':
        name = target_name(target)
        app_mutes[name] = not app_mutes.get(name, False)
        touched_apps.add(name)
        for index, (stream_name, _) in list(tracked_streams.items()):
            if stream_name == name:
                pulse_ctl.sink_input_mute(index, app_mutes[name])
        save_pending.set()
    elif kind == 'default_sink':
        if default_sink:
            index = default_sink[0]
            pulse_ctl.sink_mute(index, not pulse_ctl.sink_info(index).mute)
    elif kind == 'default_source':
        if default_source:
            index = default_source[0]
            pulse_ctl.source_mute(index, not pulse_ctl.source_info(index).mute)
    elif kind == 'sink':
        device = resolve_device('sink', target_name(target))
        if device:
            pulse_ctl.sink_mute(device[0], not pulse_ctl.sink_info(device[0]).mute)
    elif kind == 'source':
        device = resolve_device('source', target_name(target))
        if device:
            pulse_ctl.source_mute(device[0], not pulse_ctl.source_info(device[0]).mute)
    notify_tray()


def target_is_muted(target):
    """Only used to draw the check marks in the tray menu."""
    kind = target_kind(target)
    try:
        if kind == 'app':
            return app_mutes.get(target_name(target), False)
        if kind == 'default_sink' and default_sink:
            return bool(pulse_ctl.sink_info(default_sink[0]).mute)
        if kind == 'default_source' and default_source:
            return bool(pulse_ctl.source_info(default_source[0]).mute)
        if kind == 'sink':
            device = resolve_device('sink', target_name(target))
            return bool(pulse_ctl.sink_info(device[0]).mute) if device else False
        if kind == 'source':
            device = resolve_device('source', target_name(target))
            return bool(pulse_ctl.source_info(device[0]).mute) if device else False
    except pulsectl.PulseError:
        return False
    return False


# --- Stream restore ----------------------------------------------------------

def update_stream_restore():
    """Store the current levels in the server's stream-restore database.

    This makes a *newly created* stream start at the right volume instead of
    starting loud and being pulled down afterwards. Spotify tears down and
    recreates its stream on track changes, so this is what removes the audible
    jump - the stream is simply never loud in the first place.
    Best effort: the extension is not available on every server.
    """
    for name in sorted(touched_apps):
        if name not in app_levels:
            continue
        entry = pulsectl.PulseExtStreamRestoreInfo(
            'sink-input-by-application-name:%s' % name,
            volume=app_levels[name] / 127.0,
            channel_list=['mono'],
            mute=app_mutes.get(name, False))
        pulse_ctl.stream_restore_write(entry, mode='replace')


def save_thread():
    global stream_restore_supported
    while True:
        save_pending.wait()
        # Debounce: writing on every single fader tick would hammer the disk
        # while dragging.
        time.sleep(SAVE_DEBOUNCE_SECONDS)
        save_pending.clear()
        with state_lock:
            levels_snapshot = dict(app_levels)
            mutes_snapshot = dict(app_mutes)
            if pulse_ctl is not None and stream_restore_supported:
                try:
                    update_stream_restore()
                except Exception as err:
                    stream_restore_supported = False
                    print("stream-restore unavailable, disabling it:", err)
        try:
            atomic_write_json(STATE_PATH, {'levels': levels_snapshot,
                                           'mutes': mutes_snapshot})
        except OSError as err:
            print("could not write state:", err)


# --- Event handling ----------------------------------------------------------

def on_pulse_event(event):
    # Runs on the listener connection: no pulse calls allowed in here, so the
    # event is only handed over to the worker thread.
    event_queue.put((event.facility, event.t, event.index))


def handle_event(facility, event_type, index):
    if facility == 'server':
        refresh_defaults()
        device_cache.clear()
        return
    if facility in ('sink', 'source'):
        # Indices are not stable across device changes - drop the cache and
        # resolve lazily again on the next access.
        device_cache.clear()
        if event_type != 'remove':
            refresh_defaults()
        return
    if event_type == 'remove':
        tracked_streams.pop(index, None)
        return
    if event_type == 'new':
        try:
            info = pulse_ctl.sink_input_info(index)
        except pulsectl.PulseIndexError:
            return
        name = match_app(info)
        if name:
            tracked_streams[index] = (name, len(info.volume.values))
            adopt_unknown_app(name, info)
            enforce_stream(index, info)
        return
    if index in tracked_streams:
        enforce_stream(index)


def event_worker():
    while True:
        facility, event_type, index = event_queue.get()
        with state_lock:
            if pulse_ctl is None:
                continue
            try:
                handle_event(facility, event_type, index)
            except pulsectl.PulseError as err:
                print("pulse error while handling event:", err)


def connect_ctl():
    global pulse_ctl
    pulse_ctl = pulsectl.Pulse('nanokontrol-ctl')
    device_cache.clear()
    refresh_defaults()
    rescan_streams()


def event_thread():
    """Owns the second connection, which does nothing but listen. Reconnects
    on its own if the sound server restarts."""
    global pulse_ctl
    while True:
        pulse_evt = None
        try:
            pulse_evt = pulsectl.Pulse('nanokontrol-events')
            pulse_evt.event_mask_set('sink_input', 'sink', 'source', 'server')
            pulse_evt.event_callback_set(on_pulse_event)
            with state_lock:
                connect_ctl()
            print("connected to sound server")
            notify_tray()
            pulse_evt.event_listen()   # blocks until the connection dies
        except Exception as err:
            print("pulse connection lost (%s), retrying in 2s" % err)
        finally:
            if pulse_evt is not None:
                try:
                    pulse_evt.close()
                except Exception:
                    pass
            with state_lock:
                if pulse_ctl is not None:
                    try:
                        pulse_ctl.close()
                    except Exception:
                        pass
                    pulse_ctl = None
                tracked_streams.clear()
                device_cache.clear()
            notify_tray()
            time.sleep(2)


# --- MIDI --------------------------------------------------------------------

def run_transport(command):
    argv = ['playerctl']
    player = (config.get('transport_player') or '').strip()
    if player:
        argv.append('--player=%s' % player)
    argv.append(command)
    # Fired off in its own thread so the MIDI callback is never blocked by the
    # process spawn.
    threading.Thread(target=subprocess.run, args=(argv,),
                     kwargs={'check': False}, daemon=True).start()


def dispatch(controller, value):
    control = CC_TO_CONTROL.get(controller)
    if control is not None:
        target = config['assignments'].get(control, 'none')
        if target_kind(target) != 'none':
            set_target_volume(target, value)
        return
    if controller in MUTE_CC_TO_CONTROL and value == 127:
        target = config['assignments'].get(MUTE_CC_TO_CONTROL[controller], 'none')
        if target_kind(target) != 'none':
            toggle_target_mute(target)
        return
    if controller in TRANSPORT_CC and value == 127:
        run_transport(TRANSPORT_CC[controller])


def midi_callback(event, data=None):
    message, _ = event
    if len(message) != 3:
        return
    status, controller, value = message

    if TEST_MODE:
        print(f"channel={status & 0x0F} controller={controller} value={value}")
        return

    with state_lock:
        if pulse_ctl is None:
            return
        try:
            dispatch(controller, value)
        except pulsectl.PulseError as err:
            print("pulse error:", err)


# --- Keep-alive player -------------------------------------------------------

class KeepAlive:
    """Plays the keep-alive burst on the default output device (PortAudio's
    ALSA "default", which is PipeWire/PulseAudio). sounddevice is imported
    and used only in this thread."""

    def __init__(self):
        self.wake = threading.Event()
        self.locked = False
        self.test_settings = None
        self.sd = None
        self.tone_key = None
        self.tone = b''
        self.status = 'off'
        self.last_error = None

    # -- called from other threads --

    def settings_changed(self):
        self.wake.set()

    def set_locked(self, locked):
        if locked != self.locked:
            self.locked = locked
            print("session %s" % ("locked" if locked else "unlocked"))
            self.wake.set()

    def test(self, settings):
        """Play one burst with these (unsaved) settings right away."""
        self.test_settings = dict(settings)
        self.wake.set()

    # -- keep-alive thread --

    def _set_status(self, text):
        if text != self.status:
            self.status = text
            notify_tray()

    def _play(self, settings):
        key = tuple(sorted(settings.items()))
        if key != self.tone_key:
            self.tone = build_keepalive_tone(settings)
            self.tone_key = key
        if self.sd is None:
            import sounddevice
            self.sd = sounddevice
        for attempt in (1, 2):
            try:
                with self.sd.RawOutputStream(samplerate=settings['sample_rate'],
                                             channels=1, dtype='float32') as stream:
                    stream.write(self.tone)
                return
            except Exception:
                if attempt == 2:
                    raise
                # PortAudio's device list may be outdated: re-initialise once.
                try:
                    self.sd._terminate()
                    self.sd._initialize()
                except Exception:
                    pass

    def _safe_play(self, settings, what):
        try:
            self._play(settings)
        except Exception as err:
            message = "keep-alive %s failed: %r" % (what, err)
            if message != self.last_error:     # do not flood the journal
                print(message)
                self.last_error = message
            self._set_status("error: %s" % err)
            return False
        if self.last_error is not None:
            print("keep-alive playing again")
            self.last_error = None
        return True

    def run(self):
        last_burst = None             # monotonic start of the last burst
        while True:
            try:
                test, self.test_settings = self.test_settings, None
                if test is not None:
                    self._safe_play(test, "test")

                with state_lock:
                    settings = dict(config['keepalive'])
                paused = settings['pause_when_locked'] and self.locked
                if not settings['enabled'] or paused:
                    # Re-activation starts with a burst right away.
                    last_burst = None
                    self._set_status("paused (PC locked)" if settings['enabled'] else "off")
                    timeout = None    # sleep until something changes
                else:
                    interval = settings['interval_minutes'] * 60
                    now = time.monotonic()
                    if last_burst is None or now - last_burst >= interval:
                        last_burst = now
                        if self._safe_play(settings, "burst"):
                            self._set_status("active, last burst %s" % time.strftime('%H:%M'))
                    timeout = max(0.0, last_burst + interval - time.monotonic())
            except Exception as err:      # never let this thread die
                print("keep-alive error:", err)
                timeout = 30.0
            self.wake.wait(timeout)
            self.wake.clear()


class LockMonitor:
    """Follows the LockedHint of the user's graphical session in logind.
    Desktops (GNOME, KDE, ...) set it when the screen is locked; logind
    announces the change as a D-Bus signal, so there is no polling. Needs the
    GLib main loop, i.e. the tray."""

    BUS_NAME = 'org.freedesktop.login1'
    SESSION_IFACE = 'org.freedesktop.login1.Session'

    def __init__(self, on_change):
        self.on_change = on_change
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        user_path = self._call('/org/freedesktop/login1', 'org.freedesktop.login1.Manager',
                               'GetUser', GLib.Variant('(u)', (os.getuid(),)), '(o)')[0]
        # The service is not part of the session itself (systemd user
        # service), so the session is taken from the user's display session.
        self.session_path = self._get(user_path, 'org.freedesktop.login1.User', 'Display')[1]
        if not self.session_path or self.session_path == '/':
            raise RuntimeError("no graphical session found")
        self.bus.signal_subscribe(self.BUS_NAME, 'org.freedesktop.DBus.Properties',
                                  'PropertiesChanged', self.session_path, None,
                                  Gio.DBusSignalFlags.NONE, self._on_properties, None)
        self.on_change(bool(self._get(self.session_path, self.SESSION_IFACE, 'LockedHint')))

    def _call(self, path, interface, method, params, reply_type):
        return self.bus.call_sync(self.BUS_NAME, path, interface, method, params,
                                  GLib.VariantType(reply_type), Gio.DBusCallFlags.NONE,
                                  2000, None).unpack()

    def _get(self, path, interface, prop):
        return self._call(path, 'org.freedesktop.DBus.Properties', 'Get',
                          GLib.Variant('(ss)', (interface, prop)), '(v)')[0]

    def _on_properties(self, _conn, _sender, _path, _iface, _signal, params, _data):
        interface, changed, _invalidated = params.unpack()
        if interface == self.SESSION_IFACE and 'LockedHint' in changed:
            self.on_change(bool(changed['LockedHint']))


# --- Choices for the settings UI --------------------------------------------

def collect_choices():
    """Build the list of assignable targets. Running streams only show what is
    playing right now, so the stream-restore database and the stored known_apps
    are folded in - otherwise a closed application could never be assigned."""
    apps = set(config.get('known_apps') or []) | set(assigned_apps) | set(app_levels)
    sinks, sources = [], []
    with state_lock:
        if pulse_ctl is not None:
            try:
                for sink_input in pulse_ctl.sink_input_list():
                    name = sink_input.proplist.get('application.name')
                    if name:
                        apps.add(name)
                for sink in pulse_ctl.sink_list():
                    label = sink.description or sink.name
                    sinks.append((sink.name, label))
                    device_labels[sink.name] = label
                for source in pulse_ctl.source_list():
                    if source.proplist.get('device.class') == 'monitor':
                        continue
                    label = source.description or source.name
                    sources.append((source.name, label))
                    device_labels[source.name] = label
            except pulsectl.PulseError as err:
                print("could not enumerate devices:", err)
            try:
                prefix = 'sink-input-by-application-name:'
                for entry in pulse_ctl.stream_restore_list():
                    if entry.name.startswith(prefix):
                        apps.add(entry.name[len(prefix):])
            except Exception:
                pass   # extension not available, no problem

    choices = [('none', "— unassigned —"),
               ('default_sink', "Master (default output)"),
               ('default_source', "Microphone (default input)")]
    for name in sorted(apps, key=str.lower):
        choices.append(('app:%s' % name, "Application: %s" % name))
    for name, label in sorted(sinks, key=lambda item: item[1].lower()):
        choices.append(('sink:%s' % name, "Output: %s" % label))
    for name, label in sorted(sources, key=lambda item: item[1].lower()):
        choices.append(('source:%s' % name, "Input: %s" % label))
    return choices


def list_mpris_players():
    try:
        result = subprocess.run(['playerctl', '-l'], check=False, timeout=2,
                                capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# --- Tray --------------------------------------------------------------------

def notify_tray():
    if tray is not None:
        GLib.idle_add(tray.rebuild_menu)


class TrayApp:
    def __init__(self):
        self.indicator = AppIndicator.Indicator.new(
            'nanokontrol-daemon', 'audio-volume-high-symbolic',
            AppIndicator.IndicatorCategory.HARDWARE)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title('nanoKONTROL2')
        self.settings_window = None
        self.keepalive_window = None
        self.updating_menu = False
        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self.rebuild_menu()

    def rebuild_menu(self):
        # set_active() below fires "toggled" as well, so the handler has to be
        # able to tell a programmatic update from a real click.
        self.updating_menu = True
        for child in self.menu.get_children():
            self.menu.remove(child)

        with state_lock:
            connected = pulse_ctl is not None
            entries = []
            for index in range(8):
                target = config['assignments'].get('fader%d' % index, 'none')
                if target_kind(target) == 'none':
                    continue
                entries.append((target, target_label(target),
                                target_is_muted(target) if connected else False))

        header = Gtk.MenuItem(label="nanoKONTROL2" if connected
                              else "nanoKONTROL2 (not connected)")
        header.set_sensitive(False)
        self.menu.append(header)
        self.menu.append(Gtk.SeparatorMenuItem())

        if entries:
            for target, label, muted in entries:
                item = Gtk.CheckMenuItem(label="Mute: %s" % label)
                item.set_active(muted)
                item.connect('toggled', self.on_mute_toggled, target)
                self.menu.append(item)
        else:
            empty = Gtk.MenuItem(label="No faders assigned")
            empty.set_sensitive(False)
            self.menu.append(empty)

        self.menu.append(Gtk.SeparatorMenuItem())
        with state_lock:
            keepalive_on = config['keepalive']['enabled']
        keepalive_item = Gtk.CheckMenuItem(label="Speaker keep-alive")
        keepalive_item.set_active(keepalive_on)
        keepalive_item.connect('toggled', self.on_keepalive_toggled)
        self.menu.append(keepalive_item)

        self.menu.append(Gtk.SeparatorMenuItem())
        settings_item = Gtk.MenuItem(label="Settings …")
        settings_item.connect('activate', self.on_settings)
        self.menu.append(settings_item)

        keepalive_settings_item = Gtk.MenuItem(label="Keep-alive settings …")
        keepalive_settings_item.connect('activate', self.on_keepalive_settings)
        self.menu.append(keepalive_settings_item)

        rescan_item = Gtk.MenuItem(label="Rescan devices")
        rescan_item.connect('activate', self.on_rescan)
        self.menu.append(rescan_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect('activate', self.on_quit)
        self.menu.append(quit_item)

        self.menu.show_all()
        self.updating_menu = False
        if self.keepalive_window is not None and self.keepalive_window.get_visible():
            self.keepalive_window.update_status()
        return False   # so this can be used with GLib.idle_add

    def on_mute_toggled(self, item, target):
        if self.updating_menu:
            return
        with state_lock:
            if pulse_ctl is None:
                return
            if target_is_muted(target) != item.get_active():
                toggle_target_mute(target)

    def on_rescan(self, _item):
        with state_lock:
            if pulse_ctl is not None:
                try:
                    device_cache.clear()
                    device_labels.clear()
                    refresh_defaults()
                    rescan_streams()
                except pulsectl.PulseError as err:
                    print("rescan failed:", err)
        self.rebuild_menu()

    def on_settings(self, _item):
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self)
        self.settings_window.reload()
        self.settings_window.show_all()
        self.settings_window.present()

    def on_keepalive_toggled(self, item):
        if self.updating_menu:
            return
        with state_lock:
            values = dict(config['keepalive'])
        values['enabled'] = item.get_active()
        save_keepalive(values)
        if self.keepalive_window is not None and self.keepalive_window.get_visible():
            self.keepalive_window.enabled.set_active(values['enabled'])

    def on_keepalive_settings(self, _item):
        if self.keepalive_window is None:
            self.keepalive_window = KeepAliveWindow()
        if not self.keepalive_window.get_visible():
            self.keepalive_window.reload()
        self.keepalive_window.show_all()
        self.keepalive_window.present()

    def on_quit(self, _item):
        Gtk.main_quit()


def save_keepalive(values):
    with state_lock:
        config['keepalive'] = normalize_keepalive(values)
        try:
            save_config()
        except OSError as err:
            print("could not write config:", err)
    print("keep-alive settings saved:", "active" if values['enabled'] else "off")
    if keepalive is not None:
        keepalive.settings_changed()


class KeepAliveWindow(Gtk.Window):
    """Settings of the speaker keep-alive. Values only take effect on Save;
    Test plays the values as they are in the window."""

    def __init__(self):
        super().__init__(title="nanoKONTROL2 – Speaker keep-alive")
        self.set_border_width(12)
        self.set_resizable(False)
        self.connect('delete-event', self.on_close)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add(outer)

        intro = Gtk.Label(
            label="Plays a short, quiet burst on the default output device at a fixed\n"
                  "interval, so that active speakers do not fall into standby.\n"
                  "The volume and mute of the output device apply to the burst as well.",
            xalign=0)
        outer.pack_start(intro, False, False, 0)

        self.enabled = Gtk.CheckButton(label="Keep-alive active")
        outer.pack_start(self.enabled, False, False, 0)

        grid = Gtk.Grid(row_spacing=6, column_spacing=12)
        outer.pack_start(grid, False, False, 0)
        self.spins = {}
        rows = (
            ('interval_minutes', "Interval (start to start), min", 0.5, 1),
            ('duration_s', "Burst length, s", 0.1, 2),
            ('frequency_hz', "Frequency, Hz", 100.0, 0),
            ('level_dbfs', "Level, dBFS (0 = full scale)", 1.0, 1),
            ('fade_s', "Fade in/out, s", 0.01, 3),
        )
        row = 0
        for key, text, step, digits in rows:
            low, high = KEEPALIVE_LIMITS[key]
            spin = Gtk.SpinButton.new_with_range(low, high, step)
            spin.set_digits(digits)
            spin.set_hexpand(True)
            spin.set_tooltip_text("Default: %s" % KEEPALIVE_DEFAULTS[key])
            self.spins[key] = spin
            grid.attach(Gtk.Label(label=text, xalign=0), 0, row, 1, 1)
            grid.attach(spin, 1, row, 1, 1)
            row += 1
        self.sample_rate = Gtk.ComboBoxText()
        for rate in KEEPALIVE_SAMPLE_RATES:
            self.sample_rate.append(str(rate), "%d Hz" % rate)
        grid.attach(Gtk.Label(label="Sample rate", xalign=0), 0, row, 1, 1)
        grid.attach(self.sample_rate, 1, row, 1, 1)

        self.pause_locked = Gtk.CheckButton(label="Pause while the PC is locked")
        outer.pack_start(self.pause_locked, False, False, 0)

        self.status = Gtk.Label(xalign=0)
        outer.pack_start(self.status, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        test_button = Gtk.Button(label="Test now")
        test_button.set_tooltip_text("Plays one burst with the values shown, without saving them.")
        test_button.connect('clicked', self.on_test)
        buttons.pack_start(test_button, False, False, 0)
        reset_button = Gtk.Button(label="Reset to defaults")
        reset_button.connect('clicked', lambda _b: self.load(KEEPALIVE_DEFAULTS))
        buttons.pack_start(reset_button, False, False, 0)
        close_button = Gtk.Button(label="Close")
        close_button.connect('clicked', lambda _b: self.hide())
        buttons.pack_end(close_button, False, False, 0)
        save_button = Gtk.Button(label="Save")
        save_button.get_style_context().add_class('suggested-action')
        save_button.connect('clicked', self.on_save)
        buttons.pack_end(save_button, False, False, 0)
        outer.pack_start(buttons, False, False, 0)

    def load(self, settings):
        self.enabled.set_active(settings['enabled'])
        for key, spin in self.spins.items():
            spin.set_value(settings[key])
        self.sample_rate.set_active_id(str(settings['sample_rate']))
        self.pause_locked.set_active(settings['pause_when_locked'])

    def values(self):
        values = {key: spin.get_value() for key, spin in self.spins.items()}
        values['enabled'] = self.enabled.get_active()
        values['sample_rate'] = int(self.sample_rate.get_active_id() or KEEPALIVE_DEFAULTS['sample_rate'])
        values['pause_when_locked'] = self.pause_locked.get_active()
        return values

    def reload(self):
        with state_lock:
            self.load(dict(config['keepalive']))
        self.update_status()

    def update_status(self):
        status = keepalive.status if keepalive is not None else 'off'
        self.status.set_text("Status: %s" % status)

    def _checked_values(self):
        # Spin buttons only commit typed text on focus loss.
        for spin in self.spins.values():
            spin.update()
        values = self.values()
        problem = keepalive_problem(values)
        if problem:
            dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                       message_type=Gtk.MessageType.WARNING,
                                       buttons=Gtk.ButtonsType.OK, text=problem)
            dialog.run()
            dialog.destroy()
            return None
        return values

    def on_test(self, _button):
        values = self._checked_values()
        if values is not None and keepalive is not None:
            keepalive.test(values)

    def on_save(self, _button):
        values = self._checked_values()
        if values is None:
            return
        save_keepalive(values)
        self.hide()

    def on_close(self, _window, _event):
        self.hide()
        return True   # keep the window alive for the next open


class SettingsWindow(Gtk.Window):
    def __init__(self, tray_app):
        super().__init__(title="nanoKONTROL2 – Assignments")
        self.tray_app = tray_app
        self.set_default_size(620, 700)
        self.set_border_width(12)
        self.combos = {}
        self.connect('delete-event', self.on_close)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(outer)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        outer.pack_start(scroller, True, True, 0)

        self.grid = Gtk.Grid(row_spacing=6, column_spacing=12)
        scroller.add(self.grid)

        row = self._add_heading(0, "Faders (the group's mute button follows its fader)")
        for index, cc in enumerate(FADER_CCS):
            row = self._add_control_row(row, 'fader%d' % index,
                                        "Fader %d (CC %d)" % (index + 1, cc))
        row = self._add_heading(row, "Knobs")
        for index, cc in enumerate(KNOB_CCS):
            row = self._add_control_row(row, 'knob%d' % index,
                                        "Knob %d (CC %d)" % (index + 1, cc))

        row = self._add_heading(row, "Transport (play/pause, previous, next)")
        label = Gtk.Label(label="MPRIS player", xalign=0)
        self.player_combo = Gtk.ComboBoxText.new_with_entry()
        self.player_combo.set_hexpand(True)
        self.grid.attach(label, 0, row, 1, 1)
        self.grid.attach(self.player_combo, 1, row, 1, 1)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_app = Gtk.Button(label="Add application …")
        add_app.connect('clicked', self.on_add_app)
        buttons.pack_start(add_app, False, False, 0)

        close_button = Gtk.Button(label="Close")
        close_button.connect('clicked', lambda _b: self.hide())
        buttons.pack_end(close_button, False, False, 0)

        save_button = Gtk.Button(label="Save")
        save_button.get_style_context().add_class('suggested-action')
        save_button.connect('clicked', self.on_save)
        buttons.pack_end(save_button, False, False, 0)
        outer.pack_start(buttons, False, False, 0)

    def _add_heading(self, row, text):
        label = Gtk.Label(xalign=0)
        label.set_markup("<b>%s</b>" % GLib.markup_escape_text(text))
        label.set_margin_top(12 if row else 0)
        self.grid.attach(label, 0, row, 2, 1)
        return row + 1

    def _add_control_row(self, row, control, text):
        label = Gtk.Label(label=text, xalign=0)
        combo = Gtk.ComboBoxText()
        combo.set_hexpand(True)
        self.combos[control] = combo
        self.grid.attach(label, 0, row, 1, 1)
        self.grid.attach(combo, 1, row, 1, 1)
        return row + 1

    def reload(self):
        choices = collect_choices()
        with state_lock:
            assignments = dict(config['assignments'])
            player = config.get('transport_player', '')
        for control, combo in self.combos.items():
            current = assignments.get(control, 'none')
            combo.remove_all()
            ids = set()
            for target_id, label in choices:
                combo.append(target_id, label)
                ids.add(target_id)
            # An assignment can point at something absent right now (device
            # unplugged, app never seen). Keep it selectable so that saving
            # does not silently drop it.
            if current not in ids:
                combo.append(current, "%s (currently unavailable)" % target_label(current))
            combo.set_active_id(current)

        self.player_combo.remove_all()
        players = list_mpris_players()
        if player and player not in players:
            players.insert(0, player)
        for name in players:
            self.player_combo.append_text(name)
        self.player_combo.get_child().set_text(player)

    def on_add_app(self, _button):
        dialog = Gtk.Dialog(title="Add application", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Add", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        box.add(Gtk.Label(
            label="Application name, exactly as reported in the\n"
                  "application.name property (e.g. \"Spotify\", \"Firefox\").",
            xalign=0))
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        box.add(entry)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        response = dialog.run()
        name = entry.get_text().strip()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and name:
            with state_lock:
                config['known_apps'] = sorted(set(config.get('known_apps') or []) | {name})
                try:
                    save_config()
                except OSError as err:
                    print("could not write config:", err)
            self.reload()

    def on_save(self, _button):
        assignments = {}
        for control, combo in self.combos.items():
            target = combo.get_active_id() or 'none'
            if target_kind(target) != 'none':
                assignments[control] = target
        player = self.player_combo.get_child().get_text().strip()

        with state_lock:
            config['assignments'] = assignments
            config['transport_player'] = player
            refresh_assigned_apps()
            try:
                save_config()
            except OSError as err:
                print("could not write config:", err)
            # Newly assigned apps have to be picked up immediately, dropped
            # ones have to stop being enforced.
            if pulse_ctl is not None:
                try:
                    rescan_streams()
                except pulsectl.PulseError as err:
                    print("rescan after save failed:", err)
        self.tray_app.rebuild_menu()
        self.hide()

    def on_close(self, _window, _event):
        self.hide()
        return True   # keep the window alive for the next open


# --- Startup -----------------------------------------------------------------

def open_midi():
    midi_in = rtmidi.MidiIn()
    ports = midi_in.get_ports()
    port_index = next((i for i, port in enumerate(ports) if "nanoKONTROL2" in port), None)
    if port_index is None:
        raise SystemExit("no nanoKONTROL2 MIDI port found, available: %s" % ports)
    midi_in.open_port(port_index)
    midi_in.set_callback(midi_callback)
    return midi_in


def main():
    global tray, keepalive

    load_config()
    load_state()

    midi_in = open_midi()   # kept referenced so the port stays open

    if not TEST_MODE:
        threading.Thread(target=event_worker, daemon=True).start()
        threading.Thread(target=event_thread, daemon=True).start()
        threading.Thread(target=save_thread, daemon=True).start()
        keepalive = KeepAlive()
        threading.Thread(target=keepalive.run, daemon=True).start()

    print("Listening for nanoKONTROL2 input... (Ctrl+C to stop)")

    if TRAY_AVAILABLE and not TEST_MODE:
        tray = TrayApp()
        try:
            LockMonitor(keepalive.set_locked)
        except Exception as err:
            print("lock state unavailable, 'pause while locked' has no effect:", err)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, Gtk.main_quit)
        Gtk.main()
    else:
        if not TEST_MODE:
            print("tray unavailable (%s), running headless" % TRAY_IMPORT_ERROR)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass

    with state_lock:
        try:
            save_state()
        except OSError as err:
            print("could not write state on exit:", err)
    midi_in.close_port()


if __name__ == "__main__":
    main()
