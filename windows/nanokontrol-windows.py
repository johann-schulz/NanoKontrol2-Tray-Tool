#!/usr/bin/env python3
"""nanoKONTROL2 -> Windows 11 mixer + transport control.

Windows port of nanokontrol-daemon.py. Same idea, same config format, same
controls:
  * faders and knobs control per-application volumes (Windows audio sessions)
    or audio devices (default output/input or any specific device)
  * mute buttons always follow the fader of the same group
  * play/pause, previous, next go through the Windows media controls and can
    be locked to one player
  * assignments are configurable at runtime from a tray icon
  * fader levels and mute states of applications survive a restart

Built to run for months without attention. Every part that touches the
outside world (MIDI, audio, media controls) lives in its own supervised
thread, recovers on its own and never takes the rest down with it:
  * the MIDI port is re-acquired after unplugging, standby, or when another
    program held it - no restart, no re-configuration
  * audio devices are re-resolved by id and, failing that, by name, so a
    device that comes back (even on another USB port) just works again
  * assignments are never dropped because an application or device is
    absent at the moment; the config is written atomically with a backup
  * a watchdog restarts the whole process if a thread ever hangs, and a
    small supervisor process restarts it if it ever crashes

Config lives in %APPDATA%\\nanokontrol\\{config,state}.json.
"""

import argparse
import asyncio
import copy
import ctypes
import importlib.util
import json
import logging
import logging.handlers
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

IS_WINDOWS = os.name == 'nt'
if IS_WINDOWS:
    from ctypes import wintypes


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
# Volume difference below which a session counts as "already correct". Every
# write we do is read back on the next pass, this keeps us from fighting
# float rounding.
VOLUME_TOLERANCE = 0.005

# Delay after the last change before state.json is written.
SAVE_DEBOUNCE_SECONDS = 0.4

# How often audio sessions and devices are re-read and enforced. This is the
# Windows equivalent of the PulseAudio event subscription, see the README for
# why a cheap reconcile pass is used here instead of COM callbacks.
RECONCILE_INTERVAL = 1.0

# How often the MIDI device list is checked for the controller coming and
# going.
MIDI_POLL_INTERVAL = 1.0

# A wall clock jump larger than this (beyond the expected interval) means the
# machine was asleep: MIDI is re-opened and audio re-scanned.
RESUME_GAP_SECONDS = 8.0

# A supervised thread that has not reported in for this long is considered
# hung; the watchdog then restarts the whole process.
HANG_LIMIT_SECONDS = 90.0
WATCHDOG_INTERVAL = 5.0

# Upper bound for every call into the Windows media controls.
MEDIA_TIMEOUT_SECONDS = 3.0

# Exit codes between the service process and its supervisor.
EXIT_OK = 0
EXIT_HANG = 2            # watchdog fired -> restart
EXIT_FATAL = 3           # missing dependency etc. -> do not restart
EXIT_ALREADY_RUNNING = 4  # another service process holds the lock

# Identifies our windows to the taskbar, see run_ui().
APP_USER_MODEL_ID = 'nanokontrol2.mixer.windows'

# --- Paths ---
CONFIG_DIR = os.path.join(
    os.environ.get('APPDATA') or os.path.expanduser('~'), 'nanokontrol')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.json')
STATE_PATH = os.path.join(CONFIG_DIR, 'state.json')
LOG_PATH = os.path.join(CONFIG_DIR, 'nanokontrol.log')
SUPERVISOR_LOG_PATH = os.path.join(CONFIG_DIR, 'supervisor.log')

# Targets are stored as flat strings: "none", "app:<process name>",
# "sink:<device id>", "source:<device id>", "default_sink",
# "default_source". Flat keeps config entries and combo box ids identical, so
# no translation layer is needed anywhere. Application names are process
# names as shown in Task Manager > Details ("Spotify.exe"); matching ignores
# case and a trailing ".exe", so configs from the Linux version still work.
DEFAULT_CONFIG = {
    "assignments": {
        "fader0": "app:Spotify.exe",
        "fader1": "app:firefox.exe",
        "fader2": "app:Discord.exe",
        "fader7": "default_sink",
        "knob7": "default_source",
    },
    "transport_player": "Spotify",
    "known_apps": ["Spotify.exe", "firefox.exe", "Discord.exe"],
    # device id -> friendly name, remembered so that an unplugged device is
    # still shown by name and can be found again if its id changes
    "device_labels": {},
    # substring of the MIDI input port name
    "midi_port": "nanoKONTROL2",
    # re-open an idle MIDI port this often as a last line of defence against
    # handles that died silently; 0 disables it
    "midi_idle_reopen_minutes": 15,
}

log = logging.getLogger('nanokontrol')

# --- State ---
state_lock = threading.RLock()
io_lock = threading.Lock()          # serialises file writes
shutdown = threading.Event()
save_pending = threading.Event()
audio_queue = queue.Queue()

config = copy.deepcopy(DEFAULT_CONFIG)
assigned_apps = {}                  # app_key -> configured application name

app_levels = {}                     # application name -> MIDI value 0..127
app_mutes = {}                      # application name -> bool

# Written by the worker threads, read by the UI. Never contains COM objects.
runtime = {
    'audio_ok': False,
    'audio_error': 'starting',
    'midi_connected': False,
    'midi_port': None,
    'midi_status': 'starting',
    'seen_apps': set(),             # process names with an audio session now
    'devices': {'sink': {}, 'source': {}},   # kind -> {device id: label}
    'defaults': {'sink': None, 'source': None},
    'device_mutes': {},             # device target -> bool
}

heartbeats = {}                     # thread name -> monotonic timestamp
args = None
midi_input = None
media = None


def notify_ui():
    """Replaced by the UI with a thread safe Qt signal emit."""


def heartbeat(name):
    heartbeats[name] = time.monotonic()


class Throttled:
    """Logs a recurring problem once instead of every second, and once more
    when it goes away. Keeps the log readable over months of runtime."""

    def __init__(self):
        self.active = {}

    def problem(self, key, message, *fmt):
        text = message % fmt if fmt else message
        if self.active.get(key) != text:
            self.active[key] = text
            log.warning(text)

    def ok(self, key, message=None):
        if self.active.pop(key, None) is not None and message:
            log.info(message)


throttled = Throttled()


# --- Target helpers ----------------------------------------------------------

def target_kind(target):
    if not target or target == 'none':
        return 'none'
    return target.split(':', 1)[0]


def target_name(target):
    return target.split(':', 1)[1] if ':' in target else ''


def app_key(name):
    """Normalised application name: case-insensitive, ".exe" optional."""
    name = (name or '').strip().lower()
    return name[:-4] if name.endswith('.exe') else name


def device_label(device_id):
    with state_lock:
        for devices in runtime['devices'].values():
            if device_id in devices:
                return devices[device_id]
        return config.get('device_labels', {}).get(device_id, device_id)


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
    return device_label(target_name(target))


def refresh_assigned_apps():
    global assigned_apps
    assigned_apps = {app_key(target_name(t)): target_name(t)
                     for t in config['assignments'].values()
                     if target_kind(t) == 'app' and target_name(t)}


def target_is_muted(target):
    """Only used to draw the check marks in the tray menu."""
    with state_lock:
        if target_kind(target) == 'app':
            return app_mutes.get(target_name(target), False)
        return runtime['device_mutes'].get(target, False)


# --- Persistence -------------------------------------------------------------

def _is_valid_json(path):
    try:
        with open(path, encoding='utf-8') as handle:
            return isinstance(json.load(handle), dict)
    except (OSError, ValueError):
        return False


def atomic_write_json(path, data):
    """Write via temp file + rename. The previous file is kept as .bak, but
    only if it was valid - a broken file must never replace a good backup."""
    with io_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(path) and _is_valid_json(path):
            try:
                shutil.copy2(path, path + '.bak')
            except OSError as err:
                log.warning("could not back up %s: %s", path, err)
        # Virus scanners and the search indexer like to hold freshly written
        # files for a moment, which makes the rename fail on Windows.
        for attempt in range(20):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.1)


def read_json_resilient(path):
    """Return the parsed dict from path, or from its backup if path is
    damaged, or None if neither exists. A damaged file is copied aside so it
    can be inspected, and is never silently replaced by defaults."""
    missing = object()
    for candidate in (path, path + '.bak'):
        data = missing
        for attempt in range(10):
            try:
                with open(candidate, encoding='utf-8') as handle:
                    data = json.load(handle)
                break
            except FileNotFoundError:
                break
            except PermissionError as err:
                # Briefly locked (virus scanner, sync tool): try again, and
                # treat it as damaged - not as absent - if it stays locked.
                data = err
                time.sleep(0.2)
            except (OSError, ValueError) as err:
                log.error("%s is unreadable: %s", candidate, err)
                data = err
                break
        if data is missing:
            continue
        if not isinstance(data, dict):
            if not isinstance(data, Exception):
                log.error("%s does not contain a JSON object", candidate)
            if candidate == path:
                broken = '%s.broken-%s' % (path, time.strftime('%Y%m%d-%H%M%S'))
                try:
                    shutil.copy2(path, broken)
                    log.error("kept the damaged file as %s", broken)
                except OSError:
                    pass
            continue
        if candidate != path:
            log.warning("restored %s from its backup", path)
        return data
    return None


def load_config():
    global config
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    stored = read_json_resilient(CONFIG_PATH)
    if stored is not None:
        # Keys this version does not know are kept, so a newer config is not
        # stripped by an older script.
        for key, value in stored.items():
            if key not in DEFAULT_CONFIG:
                cfg[key] = value
        # Only known control ids are taken over, so a hand edited file cannot
        # inject assignments for controls that do not exist. Assignments are
        # NOT checked against what is running or plugged in right now.
        if isinstance(stored.get('assignments'), dict):
            cfg['assignments'] = {
                control: target for control, target in stored['assignments'].items()
                if control in CONTROL_TO_CC and isinstance(target, str)
                and target_kind(target) != 'none'}
        if isinstance(stored.get('transport_player'), str):
            cfg['transport_player'] = stored['transport_player']
        if isinstance(stored.get('known_apps'), list):
            cfg['known_apps'] = [n for n in stored['known_apps'] if isinstance(n, str)]
        if isinstance(stored.get('device_labels'), dict):
            cfg['device_labels'] = {k: v for k, v in stored['device_labels'].items()
                                    if isinstance(k, str) and isinstance(v, str)}
        if isinstance(stored.get('midi_port'), str) and stored['midi_port'].strip():
            cfg['midi_port'] = stored['midi_port'].strip()
        minutes = stored.get('midi_idle_reopen_minutes')
        if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes >= 0:
            cfg['midi_idle_reopen_minutes'] = minutes
    with state_lock:
        config = cfg
        refresh_assigned_apps()
    log.info("config loaded: %d assignments", len(cfg['assignments']))


def save_config():
    """Only ever called from an explicit user action (Save, Add application)."""
    with state_lock:
        known = {app_key(n): n for n in config.get('known_apps') or []}
        for key, name in assigned_apps.items():
            known.setdefault(key, name)
        config['known_apps'] = sorted(known.values(), key=str.lower)
        data = copy.deepcopy(config)
    atomic_write_json(CONFIG_PATH, data)


def load_state():
    stored = read_json_resilient(STATE_PATH)
    if stored is None:
        return
    with state_lock:
        for name, value in (stored.get('levels') or {}).items():
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 127:
                app_levels[name] = value
        for name, value in (stored.get('mutes') or {}).items():
            if isinstance(value, bool):
                app_mutes[name] = value


def save_state():
    with state_lock:
        data = {'levels': dict(app_levels), 'mutes': dict(app_mutes)}
    atomic_write_json(STATE_PATH, data)


def save_thread():
    while not shutdown.is_set():
        heartbeat('save')
        if not save_pending.wait(timeout=5):
            continue
        # Debounce: writing on every single fader tick would hammer the disk
        # while dragging.
        time.sleep(SAVE_DEBOUNCE_SECONDS)
        save_pending.clear()
        try:
            save_state()
        except OSError as err:
            throttled.problem('save', "could not write state: %s", err)
        else:
            throttled.ok('save', "state written again")


# --- Audio (Windows Core Audio) ----------------------------------------------

EDATAFLOW = {'sink': 0, 'source': 1}     # eRender, eCapture
ROLE_CONSOLE = 0
DEVICE_STATE_ACTIVE = 0x1
SESSION_STATE_EXPIRED = 2


class _Device:
    """One endpoint plus its lazily activated interfaces."""

    def __init__(self, engine, device_id, mmdevice, name):
        self.engine = engine
        self.id = device_id
        self.mmdevice = mmdevice
        self.name = name
        self._endpoint = None
        self._manager = None

    def endpoint(self):
        if self._endpoint is None:
            iface = self.mmdevice.Activate(self.engine.IAudioEndpointVolume._iid_,
                                           self.engine.comtypes.CLSCTX_ALL, None)
            self._endpoint = iface.QueryInterface(self.engine.IAudioEndpointVolume)
        return self._endpoint

    def session_manager(self):
        if self._manager is None:
            iface = self.mmdevice.Activate(self.engine.IAudioSessionManager2._iid_,
                                           self.engine.comtypes.CLSCTX_ALL, None)
            self._manager = iface.QueryInterface(self.engine.IAudioSessionManager2)
        return self._manager

    def drop(self):
        self._endpoint = None
        self._manager = None


class AudioEngine:
    """Owns every COM object. Only ever used from the audio thread, which is
    the only thread that initialises COM for audio - nothing is marshalled
    between apartments and the UI thread never touches it."""

    def __init__(self):
        import comtypes
        import psutil
        from pycaw.pycaw import (AudioUtilities, IAudioEndpointVolume,
                                 IAudioSessionControl2, IAudioSessionManager2,
                                 IMMDeviceEnumerator, ISimpleAudioVolume)
        self.comtypes = comtypes
        self.psutil = psutil
        self.AudioUtilities = AudioUtilities
        self.IAudioEndpointVolume = IAudioEndpointVolume
        self.IAudioSessionControl2 = IAudioSessionControl2
        self.IAudioSessionManager2 = IAudioSessionManager2
        self.IMMDeviceEnumerator = IMMDeviceEnumerator
        self.ISimpleAudioVolume = ISimpleAudioVolume
        self.clsid_enumerator = comtypes.GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
        self.enumerator = None
        self.devices = {'sink': {}, 'source': {}}   # kind -> {id: _Device}
        self.defaults = {'sink': None, 'source': None}
        self.sessions = []          # [(app_key, process name, ISimpleAudioVolume)]
        self.pid_names = {}
        self.last_published = None

    def reset(self):
        """Drop every COM reference. The next pass rebuilds from scratch."""
        self.sessions = []
        self.devices = {'sink': {}, 'source': {}}
        self.defaults = {'sink': None, 'source': None}
        self.enumerator = None

    # -- discovery --

    def _enum(self):
        if self.enumerator is None:
            self.enumerator = self.comtypes.CoCreateInstance(
                self.clsid_enumerator, self.IMMDeviceEnumerator,
                self.comtypes.CLSCTX_INPROC_SERVER)
        return self.enumerator

    def _friendly_name(self, mmdevice, device_id):
        try:
            name = self.AudioUtilities.CreateDevice(mmdevice).FriendlyName
            if name:
                return str(name)
        except Exception:
            pass
        return device_id

    def refresh_devices(self):
        enum = self._enum()
        for kind, flow in EDATAFLOW.items():
            current = {}
            collection = enum.EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE)
            for i in range(collection.GetCount()):
                mmdevice = collection.Item(i)
                if not mmdevice:          # NULL pointer
                    continue
                device_id = mmdevice.GetId()
                known = self.devices[kind].get(device_id)
                # A device that stayed keeps its activated interfaces, one
                # that vanished and came back gets fresh ones.
                current[device_id] = known or _Device(
                    self, device_id, mmdevice, self._friendly_name(mmdevice, device_id))
            self.devices[kind] = current
            try:
                self.defaults[kind] = enum.GetDefaultAudioEndpoint(flow, ROLE_CONSOLE).GetId()
            except Exception:
                self.defaults[kind] = None    # no device of that kind at all

    def _process_name(self, pid):
        name = self.pid_names.get(pid)
        if name is None:
            try:
                name = self.psutil.Process(pid).name()
            except Exception:
                name = ''
            self.pid_names[pid] = name
        return name

    def refresh_sessions(self):
        sessions = []
        seen = set()
        alive = set()
        for device in list(self.devices['sink'].values()):
            try:
                enum = device.session_manager().GetSessionEnumerator()
                count = enum.GetCount()
            except Exception as err:
                log.debug("session enumeration failed on %s: %s", device.name, err)
                device.drop()
                continue
            for i in range(count):
                try:
                    control = enum.GetSession(i)
                    if not control:       # NULL pointer
                        continue
                    control2 = control.QueryInterface(self.IAudioSessionControl2)
                    if control2.GetState() == SESSION_STATE_EXPIRED:
                        continue
                    pid = control2.GetProcessId()
                    if not pid:
                        continue          # system sounds
                    name = self._process_name(pid)
                    if not name:
                        continue
                    alive.add(pid)
                    volume = control2.QueryInterface(self.ISimpleAudioVolume)
                except Exception:
                    continue              # session vanished mid-enumeration
                sessions.append((app_key(name), name, volume))
                seen.add(name)
        # Forget processes that are gone, pids get reused.
        self.pid_names = {pid: n for pid, n in self.pid_names.items() if pid in alive}
        self.sessions = sessions
        with state_lock:
            runtime['seen_apps'] = seen

    # -- enforcement --

    def enforce(self):
        """Push the stored level/mute onto every session of an assigned app,
        but only where it actually deviates."""
        adopted = False
        for key, _name, volume in self.sessions:
            with state_lock:
                app = assigned_apps.get(key)
                if app is None:
                    continue
                if app not in app_levels:
                    # First time an app shows up with nothing stored for it:
                    # take its current volume as the desired one, so a fresh
                    # assignment does not yank it to some arbitrary value.
                    try:
                        app_levels[app] = int(round(volume.GetMasterVolume() * 127))
                        app_mutes.setdefault(app, bool(volume.GetMute()))
                    except Exception:
                        continue
                    adopted = True
                level = app_levels[app] / 127.0
                wanted_mute = app_mutes.get(app, False)
            try:
                if abs(volume.GetMasterVolume() - level) > VOLUME_TOLERANCE:
                    volume.SetMasterVolume(level, None)
                if bool(volume.GetMute()) != wanted_mute:
                    volume.SetMute(wanted_mute, None)
            except Exception:
                pass                      # session ended, gone next pass
        if adopted:
            save_pending.set()

    def resolve(self, target):
        """Target -> _Device, or None if it is not present right now."""
        kind = target_kind(target)
        if kind in ('default_sink', 'default_source'):
            kind = 'sink' if kind == 'default_sink' else 'source'
            device_id = self.defaults[kind]
            return self.devices[kind].get(device_id) if device_id else None
        if kind not in ('sink', 'source'):
            return None
        device_id = target_name(target)
        device = self.devices[kind].get(device_id)
        if device is not None:
            return device
        # Windows sometimes gives a re-plugged device a new id (another USB
        # port, driver update). Fall back to the remembered name, but only
        # if it is unambiguous.
        with state_lock:
            label = config.get('device_labels', {}).get(device_id)
        if label:
            matches = [d for d in self.devices[kind].values() if d.name == label]
            if len(matches) == 1:
                return matches[0]
        return None

    def _on_endpoint(self, device, action):
        try:
            return action(device.endpoint())
        except Exception:
            device.drop()                 # stale interface, re-activate once
            return action(device.endpoint())

    def set_level(self, target, midi_value):
        level = midi_value / 127.0
        if target_kind(target) == 'app':
            name = target_name(target)
            key = app_key(name)
            with state_lock:
                app_levels[name] = midi_value
            save_pending.set()
            for session_key, _name, volume in self.sessions:
                if session_key == key:
                    try:
                        volume.SetMasterVolume(level, None)
                    except Exception:
                        pass              # gone, next pass cleans up
            return
        device = self.resolve(target)
        if device is not None:
            self._on_endpoint(device, lambda ep: ep.SetMasterVolumeLevelScalar(level, None))

    def toggle_mute(self, target):
        if target_kind(target) == 'app':
            name = target_name(target)
            key = app_key(name)
            with state_lock:
                app_mutes[name] = not app_mutes.get(name, False)
                wanted = app_mutes[name]
            save_pending.set()
            for session_key, _name, volume in self.sessions:
                if session_key == key:
                    try:
                        volume.SetMute(wanted, None)
                    except Exception:
                        pass
        else:
            device = self.resolve(target)
            if device is not None:
                self._on_endpoint(device, lambda ep: ep.SetMute(not ep.GetMute(), None))
        self.publish(force=True)

    # -- snapshot for the UI --

    def publish(self, force=False):
        devices = {kind: {d.id: d.name for d in self.devices[kind].values()}
                   for kind in self.devices}
        mutes = {}
        with state_lock:
            targets = set(config['assignments'].values())
        for target in targets:
            if target_kind(target) in ('sink', 'source', 'default_sink', 'default_source'):
                device = self.resolve(target)
                if device is not None:
                    try:
                        mutes[target] = bool(self._on_endpoint(device, lambda ep: ep.GetMute()))
                    except Exception:
                        pass
        with state_lock:
            runtime['audio_ok'] = True
            runtime['audio_error'] = ''
            runtime['devices'] = devices
            runtime['defaults'] = dict(self.defaults)
            runtime['device_mutes'] = mutes
            snapshot = (repr(sorted(devices['sink'].items())), repr(sorted(devices['source'].items())),
                        repr(sorted(mutes.items())), repr(sorted(runtime['seen_apps'])))
        if force or snapshot != self.last_published:
            self.last_published = snapshot
            notify_ui()

    def reconcile(self):
        self.refresh_devices()
        self.refresh_sessions()
        self.enforce()
        self.publish()


def _com_init_mta():
    """Initialise COM for this thread as multi-threaded apartment. Must happen
    before comtypes is imported, which would otherwise pick STA."""
    sys.coinit_flags = 0
    if IS_WINDOWS:
        ctypes.windll.ole32.CoInitializeEx(None, 0)


def audio_thread():
    """Owns all audio COM objects. Commands from MIDI and UI arrive through
    audio_queue; between them the devices and sessions are reconciled."""
    _com_init_mta()
    engine = AudioEngine()
    next_pass = 0.0
    while not shutdown.is_set():
        heartbeat('audio')
        timeout = max(0.0, min(next_pass - time.monotonic(), RECONCILE_INTERVAL))
        commands = []
        try:
            commands.append(audio_queue.get(timeout=timeout))
            while True:
                commands.append(audio_queue.get_nowait())
        except queue.Empty:
            pass

        # Dragging a fader produces dozens of messages per second; only the
        # last value per target matters.
        levels = {}
        others = []
        force_pass = False
        for command in commands:
            if command[0] == 'level':
                levels.pop(command[1], None)
                levels[command[1]] = command[2]
            elif command[0] in ('rescan', 'reconcile'):
                force_pass = True
                if command[0] == 'rescan':
                    engine.reset()
            else:
                others.append(command)

        try:
            if force_pass or time.monotonic() >= next_pass:
                engine.reconcile()
                next_pass = time.monotonic() + RECONCILE_INTERVAL
                throttled.ok('audio', "audio backend working again")
            for target, value in levels.items():
                engine.set_level(target, value)
            for command in others:
                if command[0] == 'mute':
                    engine.toggle_mute(command[1])
        except Exception as err:
            throttled.problem('audio', "audio backend error, rebuilding: %r", err)
            engine.reset()
            with state_lock:
                runtime['audio_ok'] = False
                runtime['audio_error'] = str(err)
            notify_ui()
            # Retry soon, but do not spin if the audio service is down.
            next_pass = time.monotonic() + 2.0


# --- MIDI (WinMM, messages delivered to this thread's queue) ----------------

CALLBACK_THREAD = 0x00020000
MM_MIM_OPEN = 0x3C1
MM_MIM_CLOSE = 0x3C2
MM_MIM_DATA = 0x3C3
PM_NOREMOVE = 0x0000
PM_REMOVE = 0x0001
WM_USER = 0x0400
QS_ALLINPUT = 0x04FF
MWMO_INPUTAVAILABLE = 0x0004
MMSYSERR_TEXT = {
    1: "unspecified error",
    2: "device id out of range",
    4: "port is in use by another program",
    6: "no driver",
    7: "out of memory",
}


class MidiError(Exception):
    pass


class WinMM:
    """Minimal ctypes binding of the WinMM MIDI input API. Input messages are
    posted to the opening thread's message queue (CALLBACK_THREAD) instead of
    calling back into Python from a driver thread, which is the most robust
    way to receive them."""

    class MIDIINCAPSW(ctypes.Structure):
        _fields_ = [('wMid', ctypes.c_ushort), ('wPid', ctypes.c_ushort),
                    ('vDriverVersion', ctypes.c_uint),
                    ('szPname', ctypes.c_wchar * 32), ('dwSupport', ctypes.c_ulong)]

    def __init__(self):
        self.winmm = ctypes.WinDLL('winmm')
        self.user32 = ctypes.WinDLL('user32')
        self.kernel32 = ctypes.WinDLL('kernel32')
        w = self.winmm
        w.midiInGetNumDevs.argtypes = []
        w.midiInGetNumDevs.restype = ctypes.c_uint
        w.midiInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(self.MIDIINCAPSW), ctypes.c_uint]
        w.midiInGetDevCapsW.restype = ctypes.c_uint
        w.midiInOpen.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.c_uint,
                                 ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
        w.midiInOpen.restype = ctypes.c_uint
        for fn in (w.midiInStart, w.midiInStop, w.midiInReset, w.midiInClose):
            fn.argtypes = [wintypes.HANDLE]
            fn.restype = ctypes.c_uint
        u = self.user32
        u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT, wintypes.UINT]
        u.PeekMessageW.restype = wintypes.BOOL
        u.MsgWaitForMultipleObjectsEx.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                                  wintypes.DWORD, wintypes.DWORD]
        u.MsgWaitForMultipleObjectsEx.restype = wintypes.DWORD
        u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostThreadMessageW.restype = wintypes.BOOL
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def list_ports(self):
        ports = []
        for index in range(self.winmm.midiInGetNumDevs()):
            caps = self.MIDIINCAPSW()
            if self.winmm.midiInGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                ports.append(caps.szPname)
            else:
                ports.append('')
        return ports

    def open(self, index, thread_id):
        handle = wintypes.HANDLE()
        result = self.winmm.midiInOpen(ctypes.byref(handle), index, thread_id, 0, CALLBACK_THREAD)
        if result:
            raise MidiError(MMSYSERR_TEXT.get(result, "error %d" % result))
        result = self.winmm.midiInStart(handle)
        if result:
            self.winmm.midiInClose(handle)
            raise MidiError("start failed: %s" % MMSYSERR_TEXT.get(result, result))
        return handle

    def close(self, handle):
        for fn in (self.winmm.midiInStop, self.winmm.midiInReset, self.winmm.midiInClose):
            try:
                fn(handle)
            except Exception:
                pass


class MidiInput:
    """Keeps the nanoKONTROL2 port open, whatever happens to it. The port
    list is checked every second; any change (unplug, replug, another MIDI
    device appearing and shifting the indices), a resume from standby, or a
    close reported by the driver leads to a clean re-open. While the device
    is absent or held by another program it is simply retried."""

    def __init__(self):
        self.api = None
        self.thread_id = None
        self.handle = None
        self.handle_value = None
        self.port_list = None
        self.opened_at = 0.0
        self.last_activity = time.monotonic()
        self.reopen_reason = None

    def request_reopen(self, reason):
        """Thread safe."""
        self.reopen_reason = reason
        if self.api is not None and self.thread_id is not None:
            self.api.user32.PostThreadMessageW(self.thread_id, WM_USER, 0, 0)

    def _set_status(self, connected, status, port=None):
        with state_lock:
            changed = (runtime['midi_connected'], runtime['midi_status'],
                       runtime['midi_port']) != (connected, status, port)
            runtime['midi_connected'] = connected
            runtime['midi_status'] = status
            runtime['midi_port'] = port
        if changed:
            notify_ui()

    def _close(self):
        if self.handle is not None:
            self.api.close(self.handle)
        self.handle = None
        self.handle_value = None

    def _poll(self):
        ports = self.api.list_ports()
        if self.handle is not None:
            reason = self.reopen_reason
            with state_lock:
                idle_minutes = config.get('midi_idle_reopen_minutes', 0)
            idle = idle_minutes * 60
            now = time.monotonic()
            if reason is None and ports != self.port_list:
                reason = "MIDI device list changed"
            if (reason is None and idle > 0 and now - self.last_activity > idle
                    and now - self.opened_at > idle):
                log.debug("idle refresh of the MIDI port")
                reason = "idle refresh"
            if reason is None:
                return
            if reason != "idle refresh":
                log.info("re-opening MIDI port (%s)", reason)
            self._close()
        self.reopen_reason = None

        with state_lock:
            pattern = config.get('midi_port', 'nanoKONTROL2').lower()
        index = next((i for i, name in enumerate(ports) if pattern in name.lower()), None)
        self.port_list = ports
        if index is None:
            self._set_status(False, "nanoKONTROL2 not found")
            throttled.problem('midi', "no MIDI input matching %r, available: %s", pattern, ports)
            return
        try:
            self.handle = self.api.open(index, self.thread_id)
        except MidiError as err:
            self._set_status(False, "MIDI port: %s" % err, ports[index])
            throttled.problem('midi', "cannot open MIDI port %r: %s", ports[index], err)
            return
        self.handle_value = self.handle.value
        self.opened_at = time.monotonic()
        self._set_status(True, "connected", ports[index])
        throttled.ok('midi')
        log.info("MIDI port %r open", ports[index])

    def _pump(self, msg):
        user32 = self.api.user32
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == MM_MIM_DATA:
                if self.handle_value is not None and msg.wParam == self.handle_value:
                    self.last_activity = time.monotonic()
                    handle_midi(msg.lParam & 0xFFFFFFFF)
            elif msg.message == MM_MIM_CLOSE:
                if self.handle_value is not None and msg.wParam == self.handle_value:
                    # Not initiated by us: the driver dropped the device.
                    self.request_reopen("closed by the driver")

    def run(self):
        self.api = WinMM()
        self.thread_id = self.api.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # Make sure this thread has a message queue before opening the port.
        self.api.user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, PM_NOREMOVE)
        next_poll = 0.0
        try:
            while not shutdown.is_set():
                heartbeat('midi')
                self._pump(msg)
                if self.reopen_reason is not None or time.monotonic() >= next_poll:
                    self._poll()
                    next_poll = time.monotonic() + MIDI_POLL_INTERVAL
                wait_ms = int(max(0.0, next_poll - time.monotonic()) * 1000) + 1
                self.api.user32.MsgWaitForMultipleObjectsEx(
                    0, None, wait_ms, QS_ALLINPUT, MWMO_INPUTAVAILABLE)
        finally:
            self._close()
            self._set_status(False, "stopped")


def handle_midi(packed):
    """Runs on the MIDI thread. Never touches audio itself, it only queues."""
    status = packed & 0xFF
    controller = (packed >> 8) & 0x7F
    value = (packed >> 16) & 0x7F
    if status & 0xF0 != 0xB0:        # control change only
        return
    if args.midi_test:
        log.info("channel=%d controller=%d value=%d", status & 0x0F, controller, value)
        return
    with state_lock:
        control = CC_TO_CONTROL.get(controller)
        if control is not None:
            target = config['assignments'].get(control, 'none')
            if target_kind(target) != 'none':
                audio_queue.put(('level', target, value))
            return
        if controller in MUTE_CC_TO_CONTROL:
            target = config['assignments'].get(MUTE_CC_TO_CONTROL[controller], 'none')
        else:
            target = None
    # Buttons send 127 on press and 0 on release - act on press only.
    if value != 127:
        return
    if target is not None:
        if target_kind(target) != 'none':
            audio_queue.put(('mute', target))
        return
    if controller in TRANSPORT_CC:
        media.command(TRANSPORT_CC[controller])


# --- Media transport (Windows media controls) --------------------------------

VK_MEDIA = {'play-pause': 0xB3, 'next': 0xB0, 'previous': 0xB1}
KEYEVENTF_EXTENDEDKEY = 0x1
KEYEVENTF_KEYUP = 0x2


def send_media_key(command):
    """Fallback: a virtual media key. Reaches whatever Windows considers the
    active player, without player selection."""
    vk = VK_MEDIA[command]
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)


async def _bounded(awaitable):
    async def inner():
        return await awaitable
    return await asyncio.wait_for(inner(), MEDIA_TIMEOUT_SECONDS)


class MediaControl:
    """Transport buttons via the Global System Media Transport Controls -
    the Windows counterpart of MPRIS. Runs its own asyncio loop thread. The
    session manager is requested fresh for every command, so there is no
    cached state that could go stale."""

    def __init__(self):
        self.loop = None
        self.manager_class = None

    def run(self):
        try:
            from winrt.windows.media.control import \
                GlobalSystemMediaTransportControlsSessionManager as Manager
        except ImportError:
            try:
                from winsdk.windows.media.control import \
                    GlobalSystemMediaTransportControlsSessionManager as Manager
            except ImportError as err:
                Manager = None
                log.warning("Windows media controls unavailable (%s), "
                            "transport falls back to media keys", err)
        self.manager_class = Manager
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def beat():
            heartbeat('media')
            loop.call_later(2.0, beat)
        loop.call_soon(beat)
        self.loop = loop
        try:
            loop.run_until_complete(self._until_shutdown())
        finally:
            self.loop = None
            loop.close()

    async def _until_shutdown(self):
        while not shutdown.is_set():
            await asyncio.sleep(0.5)

    def command(self, command):
        """Thread safe, never blocks."""
        loop = self.loop
        if loop is None or self.manager_class is None:
            self._key_fallback(command)
            return
        try:
            asyncio.run_coroutine_threadsafe(self._command(command), loop)
        except RuntimeError:
            self._key_fallback(command)

    def _key_fallback(self, command):
        try:
            send_media_key(command)
        except Exception as err:
            log.warning("media key failed: %s", err)

    async def _find_session(self, manager):
        with state_lock:
            player = (config.get('transport_player') or '').strip().lower()
        if not player:
            return manager.get_current_session()
        for session in manager.get_sessions():
            if player in (session.source_app_user_model_id or '').lower():
                return session
        return None

    async def _command(self, command):
        try:
            manager = await _bounded(self.manager_class.request_async())
            session = await self._find_session(manager)
            if session is None:
                with state_lock:
                    locked = bool((config.get('transport_player') or '').strip())
                # Locked to a player that is not running: do nothing, so a
                # background browser tab can never steal the buttons.
                if not locked:
                    self._key_fallback(command)
                return
            if command == 'play-pause':
                await _bounded(session.try_toggle_play_pause_async())
            elif command == 'next':
                await _bounded(session.try_skip_next_async())
            elif command == 'previous':
                await _bounded(session.try_skip_previous_async())
        except Exception as err:
            log.warning("media command %s failed: %r", command, err)

    async def _list_players(self):
        manager = await _bounded(self.manager_class.request_async())
        return [s.source_app_user_model_id for s in manager.get_sessions()
                if s.source_app_user_model_id]

    def list_players(self, timeout=2.0):
        loop = self.loop
        if loop is None or self.manager_class is None:
            return []
        try:
            future = asyncio.run_coroutine_threadsafe(self._list_players(), loop)
            return future.result(timeout)
        except Exception as err:
            log.warning("could not list media players: %r", err)
            return []


# --- Threads, watchdog -------------------------------------------------------

def start_supervised(name, target):
    """Run target in a daemon thread and restart it whenever it dies."""
    def runner():
        backoff = 1.0
        while not shutdown.is_set():
            started = time.monotonic()
            heartbeat(name)
            try:
                target()
                if shutdown.is_set():
                    return
                log.error("thread %s returned unexpectedly, restarting", name)
            except Exception:
                log.exception("thread %s crashed, restarting in %.0fs", name, backoff)
            if time.monotonic() - started > 60:
                backoff = 1.0
            heartbeat(name)
            shutdown.wait(backoff)
            backoff = min(backoff * 2, 30.0)
    thread = threading.Thread(target=runner, name=name, daemon=True)
    thread.start()
    return thread


def watchdog_thread():
    """Detects standby (wall clock jump) and hung threads."""
    last_wall = time.time()
    while not shutdown.wait(WATCHDOG_INTERVAL):
        wall = time.time()
        gap = wall - last_wall
        last_wall = wall
        if gap > WATCHDOG_INTERVAL + RESUME_GAP_SECONDS or gap < -RESUME_GAP_SECONDS:
            log.info("clock jumped by %.0fs - resume from standby, re-acquiring devices", gap)
            now = time.monotonic()
            for name in list(heartbeats):
                heartbeats[name] = now
            if midi_input is not None:
                midi_input.request_reopen("resume from standby")
            audio_queue.put(('rescan',))
            continue
        now = time.monotonic()
        for name, stamp in list(heartbeats.items()):
            if now - stamp > HANG_LIMIT_SECONDS:
                # No final save here: the hung thread might hold the file
                # lock. state.json is at most SAVE_DEBOUNCE_SECONDS old.
                log.critical("thread %s hung for %.0fs, restarting the process", name, now - stamp)
                logging.shutdown()
                os._exit(EXIT_HANG)


# --- Choices for the settings UI --------------------------------------------

def collect_choices():
    """Build the list of assignable targets. Running sessions only show what
    is playing right now, so the saved known_apps and everything ever
    assigned are folded in - otherwise a closed application could never be
    assigned, and worse, an assignment would vanish from the list."""
    with state_lock:
        seen = set(runtime['seen_apps'])
        names = (list(config.get('known_apps') or []) + list(assigned_apps.values())
                 + list(app_levels) + sorted(seen))
        devices = {kind: dict(items) for kind, items in runtime['devices'].items()}
    apps = {}
    for name in names:
        apps.setdefault(app_key(name), name)
    seen_keys = {app_key(n) for n in seen}

    choices = [('none', "— unassigned —"),
               ('default_sink', "Master (default output)"),
               ('default_source', "Microphone (default input)")]
    for key, name in sorted(apps.items(), key=lambda item: item[0]):
        suffix = "  (playing)" if key in seen_keys else ""
        choices.append(('app:%s' % name, "Application: %s%s" % (name, suffix)))
    for device_id, label in sorted(devices['sink'].items(), key=lambda item: item[1].lower()):
        choices.append(('sink:%s' % device_id, "Output: %s" % label))
    for device_id, label in sorted(devices['source'].items(), key=lambda item: item[1].lower()):
        choices.append(('source:%s' % device_id, "Input: %s" % label))
    return choices


def status_lines():
    with state_lock:
        if runtime['midi_connected']:
            midi = "MIDI: %s connected" % runtime['midi_port']
        else:
            midi = "MIDI: %s" % runtime['midi_status']
        audio = "Audio: OK" if runtime['audio_ok'] else "Audio: %s" % runtime['audio_error']
        healthy = runtime['midi_connected'] and runtime['audio_ok']
    return midi, audio, healthy


# --- Tray and settings window (Qt) -------------------------------------------

def run_ui():
    """Qt is imported here, not at module level, so the supervisor process
    stays small."""
    global notify_ui
    from PySide6.QtCore import QObject, Qt, QTimer, Signal
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
    from PySide6.QtWidgets import (QApplication, QComboBox, QGridLayout, QHBoxLayout,
                                   QInputDialog, QLabel, QMenu, QMessageBox, QPushButton,
                                   QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget)

    class Bridge(QObject):
        changed = Signal()

    def make_icon(healthy):
        """Drawn in code, so no image file has to be shipped. Rendered at
        several sizes so it stays sharp in the tray, the taskbar and on
        high-DPI screens. Drawing coordinates are on a 64x64 grid."""
        icon = QIcon()
        for size in (16, 24, 32, 48, 64, 128, 256):
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.scale(size / 64.0, size / 64.0)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#2d2d2d'))
            painter.drawRoundedRect(2, 2, 60, 60, 12, 12)
            knob = QColor('#4fc3f7' if healthy else '#9e9e9e')
            for x, y in ((16, 38), (32, 20), (48, 44)):
                painter.setBrush(QColor('#8a8a8a'))
                painter.drawRect(x - 2, 10, 4, 44)
                painter.setBrush(knob)
                painter.drawRoundedRect(x - 8, y - 5, 16, 10, 3, 3)
            if not healthy:
                painter.setBrush(QColor('#e53935'))
                painter.drawEllipse(38, 38, 24, 24)
            painter.end()
            icon.addPixmap(pixmap)
        return icon

    class SettingsWindow(QWidget):
        def __init__(self, tray_app):
            super().__init__()
            self.tray_app = tray_app
            self.setWindowTitle("nanoKONTROL2 – Assignments")
            self.setWindowIcon(tray_app.icon_ok)
            self.resize(640, 740)
            self.combos = {}

            outer = QVBoxLayout(self)
            self.status = QLabel()
            outer.addWidget(self.status)

            scroller = QScrollArea()
            scroller.setWidgetResizable(True)
            inner = QWidget()
            self.grid = QGridLayout(inner)
            self.grid.setColumnStretch(1, 1)
            scroller.setWidget(inner)
            outer.addWidget(scroller, 1)

            row = self._add_heading(0, "Faders (the group's mute button follows its fader)")
            for index, cc in enumerate(FADER_CCS):
                row = self._add_control_row(row, 'fader%d' % index,
                                            "Fader %d (CC %d)" % (index + 1, cc))
            row = self._add_heading(row, "Knobs")
            for index, cc in enumerate(KNOB_CCS):
                row = self._add_control_row(row, 'knob%d' % index,
                                            "Knob %d (CC %d)" % (index + 1, cc))

            row = self._add_heading(row, "Transport (play/pause, previous, next)")
            self.grid.addWidget(QLabel("Media player"), row, 0)
            self.player_combo = QComboBox()
            self.player_combo.setEditable(True)
            self.player_combo.setToolTip(
                "Part of the player's app id, e.g. \"Spotify\". Empty: whatever "
                "Windows considers the current player.")
            self.grid.addWidget(self.player_combo, row, 1)

            buttons = QHBoxLayout()
            add_app = QPushButton("Add application …")
            add_app.clicked.connect(self.on_add_app)
            buttons.addWidget(add_app)
            buttons.addStretch(1)
            save_button = QPushButton("Save")
            save_button.setDefault(True)
            save_button.clicked.connect(self.on_save)
            buttons.addWidget(save_button)
            close_button = QPushButton("Close")
            close_button.clicked.connect(self.hide)
            buttons.addWidget(close_button)
            outer.addLayout(buttons)

        def _add_heading(self, row, text):
            label = QLabel(text)
            font = QFont(label.font())
            font.setBold(True)
            label.setFont(font)
            if row:
                label.setContentsMargins(0, 12, 0, 0)
            self.grid.addWidget(label, row, 0, 1, 2)
            return row + 1

        def _add_control_row(self, row, control, text):
            combo = QComboBox()
            combo.setMinimumContentsLength(30)
            self.combos[control] = combo
            self.grid.addWidget(QLabel(text), row, 0)
            self.grid.addWidget(combo, row, 1)
            return row + 1

        def update_status(self):
            midi, audio, _ = status_lines()
            self.status.setText("%s     %s" % (midi, audio))

        def reload(self, keep_selection=False):
            choices = collect_choices()
            with state_lock:
                assignments = dict(config['assignments'])
                player = config.get('transport_player', '')
            if keep_selection:
                # Adding an application must not throw away unsaved changes.
                for control, combo in self.combos.items():
                    assignments[control] = combo.currentData() or 'none'
                player = self.player_combo.currentText()
            for control, combo in self.combos.items():
                current = assignments.get(control, 'none')
                combo.blockSignals(True)
                combo.clear()
                ids = set()
                for target_id, label in choices:
                    combo.addItem(label, target_id)
                    ids.add(target_id)
                # An assignment can point at something absent right now
                # (device unplugged, app not running). Keep it selectable so
                # that saving never silently drops it.
                if current not in ids:
                    combo.addItem("%s (currently unavailable)" % target_label(current), current)
                combo.setCurrentIndex(max(0, combo.findData(current)))
                combo.blockSignals(False)

            players = media.list_players() if media is not None else []
            if player and player not in players:
                players.insert(0, player)
            self.player_combo.clear()
            self.player_combo.addItems(players)
            self.player_combo.setEditText(player)
            self.update_status()

        def on_add_app(self):
            name, ok = QInputDialog.getText(
                self, "Add application",
                "Process name as shown in Task Manager › Details\n"
                "(e.g. \"Spotify.exe\", \"firefox.exe\", \"Discord.exe\"):")
            name = (name or '').strip()
            if not ok or not name:
                return
            with state_lock:
                known = list(config.get('known_apps') or [])
                if app_key(name) not in {app_key(n) for n in known}:
                    known.append(name)
                config['known_apps'] = known
            try:
                save_config()
            except OSError as err:
                QMessageBox.critical(self, "nanoKONTROL2", "Could not write the config:\n%s" % err)
            self.reload(keep_selection=True)

        def on_save(self):
            assignments = {}
            for control, combo in self.combos.items():
                target = combo.currentData() or 'none'
                if target_kind(target) != 'none':
                    assignments[control] = target
            player = self.player_combo.currentText().strip()
            with state_lock:
                labels = dict(config.get('device_labels') or {})
                for target in assignments.values():
                    if target_kind(target) in ('sink', 'source'):
                        device_id = target_name(target)
                        label = runtime['devices'][target_kind(target)].get(device_id)
                        if label:
                            labels[device_id] = label
                config['assignments'] = assignments
                config['transport_player'] = player
                config['device_labels'] = labels
                refresh_assigned_apps()
            try:
                save_config()
            except OSError as err:
                QMessageBox.critical(self, "nanoKONTROL2",
                                     "Could not write the config:\n%s\n\n"
                                     "The new assignments are active until the next restart." % err)
            log.info("assignments saved: %d controls", len(assignments))
            # Newly assigned apps have to be picked up immediately, dropped
            # ones stop being enforced.
            audio_queue.put(('reconcile',))
            self.hide()

    class TrayApp(QObject):
        def __init__(self, app):
            super().__init__()
            self.app = app
            self.icon_ok = make_icon(True)
            self.icon_bad = make_icon(False)
            self.settings_window = None
            self.tray = QSystemTrayIcon(self.icon_bad)
            self.menu = QMenu()
            self.menu.aboutToShow.connect(self.rebuild_menu)
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(self.on_activated)
            self.rebuild_menu()
            self.update_status()
            # At logon the taskbar may not exist yet; keep trying.
            self.show_timer = QTimer(self)
            self.show_timer.timeout.connect(self.ensure_visible)
            self.show_timer.start(2000)
            self.ensure_visible()

        def ensure_visible(self):
            if QSystemTrayIcon.isSystemTrayAvailable() and not self.tray.isVisible():
                self.tray.show()

        def update_status(self):
            midi, audio, healthy = status_lines()
            self.tray.setIcon(self.icon_ok if healthy else self.icon_bad)
            self.tray.setToolTip("nanoKONTROL2\n%s\n%s" % (midi, audio))
            if self.settings_window is not None and self.settings_window.isVisible():
                self.settings_window.update_status()

        def rebuild_menu(self):
            self.menu.clear()
            midi, audio, _ = status_lines()
            for text in (midi, audio):
                self.menu.addAction(text).setEnabled(False)
            self.menu.addSeparator()

            with state_lock:
                entries = []
                for index in range(8):
                    target = config['assignments'].get('fader%d' % index, 'none')
                    if target_kind(target) != 'none':
                        entries.append(target)
            if entries:
                for target in entries:
                    action = self.menu.addAction("Mute: %s" % target_label(target))
                    action.setCheckable(True)
                    action.setChecked(target_is_muted(target))
                    action.triggered.connect(
                        lambda checked, t=target: self.on_mute(t, checked))
            else:
                self.menu.addAction("No faders assigned").setEnabled(False)

            self.menu.addSeparator()
            self.menu.addAction("Settings …").triggered.connect(self.on_settings)
            self.menu.addAction("Rescan / reconnect devices").triggered.connect(self.on_rescan)
            self.menu.addAction("Open config folder").triggered.connect(self.on_open_folder)
            self.menu.addAction("Quit").triggered.connect(self.on_quit)

        def on_activated(self, reason):
            if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                          QSystemTrayIcon.ActivationReason.DoubleClick):
                self.on_settings()

        def on_mute(self, target, checked):
            if target_is_muted(target) != checked:
                audio_queue.put(('mute', target))

        def on_rescan(self):
            log.info("rescan requested from the tray")
            audio_queue.put(('rescan',))
            if midi_input is not None:
                midi_input.request_reopen("requested from the tray")

        def on_open_folder(self):
            try:
                os.makedirs(CONFIG_DIR, exist_ok=True)
                os.startfile(CONFIG_DIR)
            except (OSError, AttributeError) as err:
                log.warning("could not open %s: %s", CONFIG_DIR, err)

        def on_settings(self):
            if self.settings_window is None:
                self.settings_window = SettingsWindow(self)
            if not self.settings_window.isVisible():
                self.settings_window.reload()
            self.settings_window.show()
            self.settings_window.raise_()
            self.settings_window.activateWindow()

        def on_quit(self):
            log.info("quit requested from the tray")
            shutdown.set()
            self.app.quit()

    # Without an own AppUserModelID, Windows groups our windows under
    # pythonw.exe and shows the Python icon in the taskbar instead of ours.
    if IS_WINDOWS:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                ctypes.c_wchar_p(APP_USER_MODEL_ID))
        except (OSError, AttributeError) as err:
            log.warning("could not set the AppUserModelID: %s", err)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("nanoKONTROL2")
    app.setQuitOnLastWindowClosed(False)

    tray_app = TrayApp(app)
    # Default icon for every window, dialogs included (taskbar, Alt+Tab).
    app.setWindowIcon(tray_app.icon_ok)
    bridge = Bridge()
    bridge.changed.connect(tray_app.update_status)
    # Emitting a signal is thread safe; Qt delivers it on the GUI thread.
    notify_ui = bridge.changed.emit

    # Ctrl+C in a console: Python only runs signal handlers between bytecodes,
    # the timer makes sure that happens while Qt sits in its event loop.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(500)

    code = app.exec()
    notify_ui = lambda: None    # noqa: E731 - the bridge is about to go away
    return code


# --- Startup -----------------------------------------------------------------

REQUIRED_MODULES = ('comtypes', 'pycaw', 'psutil', 'PySide6')


def setup_logging(path):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if args.debug else logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(levelname)-7s %(threadName)s: %(message)s')
    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=3, encoding='utf-8', delay=True)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if sys.stderr is not None:          # None under pythonw.exe
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    def excepthook(exc_type, exc, tb):
        log.critical("unhandled exception", exc_info=(exc_type, exc, tb))
    sys.excepthook = excepthook
    threading.excepthook = lambda hook_args: log.critical(
        "unhandled exception in %s", hook_args.thread.name if hook_args.thread else '?',
        exc_info=(hook_args.exc_type, hook_args.exc_value, hook_args.exc_traceback))


_mutexes = []


def acquire_mutex(name):
    """Named mutex as single-instance lock; released by Windows when the
    process dies, so a crash can never leave a stale lock behind."""
    if not IS_WINDOWS:
        return True
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _mutexes.append(handle)
    return True


def message_box(text):
    if IS_WINDOWS:
        ctypes.windll.user32.MessageBoxW(None, text, "nanoKONTROL2", 0x10 | 0x40000)


def run_service():
    """The actual program: worker threads plus the tray."""
    global midi_input, media
    # The previous instance may still be shutting down after a restart.
    for _ in range(20):
        if acquire_mutex("Local\\nanokontrol2-windows-service"):
            break
        time.sleep(0.5)
    else:
        log.error("another instance is already running")
        return EXIT_ALREADY_RUNNING

    missing = [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]
    if missing:
        log.critical("missing Python packages: %s - run install.ps1 again", ", ".join(missing))
        return EXIT_FATAL

    log.info("starting (Python %s, pid %d)", sys.version.split()[0], os.getpid())
    load_config()
    load_state()

    midi_input = MidiInput()
    media = MediaControl()
    start_supervised('audio', audio_thread)
    start_supervised('midi', midi_input.run)
    start_supervised('media', media.run)
    start_supervised('save', save_thread)
    threading.Thread(target=watchdog_thread, name='watchdog', daemon=True).start()

    try:
        code = run_ui()
    except Exception:
        log.exception("UI failed")
        code = 1
    shutdown.set()
    try:
        save_state()
    except OSError as err:
        log.error("could not write state on exit: %s", err)
    time.sleep(0.2)                     # let the MIDI thread close its port
    log.info("stopped (exit code %s)", code)
    return code


def supervise():
    """Keeps the service process alive - the Windows counterpart of systemd's
    Restart=on-failure. A clean Quit from the tray ends both."""
    argv = [sys.executable, os.path.abspath(__file__), '--child']
    if args.debug:
        argv.append('--debug')
    if args.midi_test:
        argv.append('--midi-test')
    backoff = 1.0
    while True:
        started = time.monotonic()
        process = None
        try:
            process = subprocess.Popen(argv, cwd=os.path.dirname(os.path.abspath(__file__)))
            code = process.wait()
        except KeyboardInterrupt:
            if process is not None:
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
            return EXIT_OK
        except OSError as err:
            log.error("could not start the service process: %s", err)
            code = 1
        if code == EXIT_OK:
            log.info("service ended normally")
            return EXIT_OK
        if code == EXIT_FATAL:
            log.critical("service cannot start, giving up - see %s", LOG_PATH)
            message_box("The nanoKONTROL2 mixer cannot start.\n\n"
                        "Details are in:\n%s\n\nRunning install.ps1 again usually fixes it." % LOG_PATH)
            return code
        if code == EXIT_ALREADY_RUNNING:
            # An orphaned service process from an earlier run is still alive.
            # Take over as soon as it is gone.
            shutdown.wait(5)
            continue
        if time.monotonic() - started > 60:
            backoff = 1.0
        log.warning("service exited with code %s, restarting in %.0fs", code, backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="nanoKONTROL2 mixer for Windows")
    parser.add_argument('--no-supervisor', action='store_true',
                        help="run in this process, without automatic restart")
    parser.add_argument('--midi-test', action='store_true',
                        help="only log incoming control changes, do not act on them")
    parser.add_argument('--debug', action='store_true', help="verbose logging")
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main():
    global args
    args = parse_args()
    if args.child or args.no_supervisor:
        setup_logging(LOG_PATH)
        if args.no_supervisor and not acquire_mutex("Local\\nanokontrol2-windows"):
            message_box("The nanoKONTROL2 mixer is already running (see the tray).")
            return EXIT_OK
        code = run_service()
        logging.shutdown()
        # Skip interpreter teardown: releasing COM objects of daemon threads
        # during finalisation is a classic source of crashes on exit.
        sys.stdout and sys.stdout.flush()
        os._exit(code)
    setup_logging(SUPERVISOR_LOG_PATH)
    if not acquire_mutex("Local\\nanokontrol2-windows"):
        message_box("The nanoKONTROL2 mixer is already running (see the tray).")
        return EXIT_OK
    return supervise()


if __name__ == "__main__":
    sys.exit(main())
