"""
Module for defining bell-and-whistles movement features.
"""
import functools
import logging
import numbers
import re
import signal
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from types import MethodType, SimpleNamespace
from weakref import WeakSet

import yaml
from bluesky.utils import ProgressBar
from ophyd.device import Device
from ophyd.ophydobj import Kind, OphydObject
from ophyd.signal import AttributeSignal, Signal
from ophyd.status import wait as status_wait

from . import utils as util
from .signal import NotImplementedSignal

try:
    import fcntl
except ImportError:
    fcntl = None

logger = logging.getLogger(__name__)
engineering_mode = True

OphydObject_whitelist = ["name", "connected", "check_value", "log"]
BlueskyInterface_whitelist = ["trigger", "read", "describe", "stage",
                              "unstage"]
Device_whitelist = ["read_attrs", "configuration_attrs", "summary",
                    "wait_for_connection", "stop", "get", "configure"]
Signal_whitelist = ["value", "put"]
Positioner_whitelist = ["settle_time", "timeout", "egu", "limits", "move",
                        "position", "moving"]


class BaseInterface:
    """
    Interface layer to attach to any Device for SLAC features.

    This class defines an API and some defaults for filtering tab-completion
    results for new users to avoid confusion. The API involves setting the
    tab_whitelist attribute on any subclass of BaseInterface. When in
    non-engineering mode, only elements on the whitelists will be displayed to
    the user.

    Attributes
    ----------
    tab_whitelist : list
        List of string regex to show in autocomplete for non-engineering mode.
    """

    tab_whitelist = (OphydObject_whitelist + BlueskyInterface_whitelist +
                     Device_whitelist + Signal_whitelist +
                     Positioner_whitelist)
    _filtered_dir_cache = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        mro = cls.mro()
        if Device in mro and mro.index(BaseInterface) > mro.index(Device):
            order = '\n    '.join(mro_cls.__name__ for mro_cls in mro)
            raise RuntimeError(
                f"{cls.__module__}.{cls.__name__} inherits from "
                f"`BaseInterface`, but does not correctly mix it in.  Device "
                f"must come *after* `BaseInterface` in the class method "
                f"resolution order (MRO).  Try changing the order of class "
                f"inheritance around or ask an expert.  Current order is:\n"
                f"    {order}"
            )

        string_whitelist = []
        for parent in cls.mro():
            if hasattr(parent, "tab_whitelist"):
                string_whitelist.extend(parent.tab_whitelist)
            if getattr(parent, "tab_component_names", False):
                for cpt_name in parent.component_names:
                    if getattr(parent, cpt_name).kind != Kind.omitted:
                        string_whitelist.append(cpt_name)

        cls._tab_regex = re.compile("|".join(sorted(set(string_whitelist))))

    def __dir__(self):
        if get_engineering_mode():
            return super().__dir__()
        elif self._filtered_dir_cache is None:
            self._init_filtered_dir_cache()
        return self._filtered_dir_cache

    def _init_filtered_dir_cache(self):
        self._filtered_dir_cache = self._get_filtered_tab_dir()

    def _get_filtered_tab_dir(self):
        return [elem
                for elem in super().__dir__()
                if self._tab_regex.fullmatch(elem)]

    def __repr__(self):
        """Simplify the ophydobject repr to avoid crazy long represenations."""
        prefix = getattr(self, 'prefix', None)
        name = getattr(self, 'name', None)
        return f"{self.__class__.__name__}({prefix}, name={name})"

    def _repr_pretty_(self, pp, cycle):
        """
        Set pretty-printing to show current status information.

        We will not leverage the feature set here, we will just use it as a
        convenient IPython entry point for rendering our device info.

        The parameter set is documented here in case we change our minds,
        since I already wrote it out before deciding on a renderer-agnostic
        approach.

        Parameters
        ----------
        pp: PrettyPrinter
            An instance of PrettyPrinter is always passed into the method.
            This is what you use to determine what gets printed.
            pp.text('text') adds non-breaking text to the output.
            pp.breakable() either adds a whitespace or breaks here.
            pp.pretty(obj) pretty prints another object.
            with pp.group(4, 'text', 'text') groups items into an intended set
            on multiple lines.
        cycle: bool
            This is True when the pretty printer detects a cycle, e.g. to help
            you avoid infinite loops. For example, your _repr_pretty_ method
            may call pp.pretty to print a sub-object, and that object might
            also call pp.pretty to print this object. Then cycle would be True
            and you know not to make any further recursive calls.
        """
        pp.text(self.format_status_info(self.status_info()))

    def format_status_info(self, status_info):
        """
        Entry point for the mini status displays in the ipython terminal.

        This can be overridden if a device wants a custom status printout.

        Parameters
        ----------
        status_info: dict
            See self.status_info method

        Returns
        -------
        status: str
            Formatted string with all relevant status information.
        """
        lines = self._status_info_lines(status_info)
        if lines:
            return '\n'.join(lines)
        else:
            return f'{self.name}: No status available'

    def _status_info_lines(self, status_info, prefix='', indent=0):
        full_name = status_info['name']
        if full_name.startswith(prefix):
            name = full_name.replace(prefix, '', 1)
        else:
            name = full_name

        if status_info['is_device']:
            # Set up a tree view
            header_lines = ['', f'{name}', '-' * len(name)]
            data_lines = []
            extra_keys = ('name', 'kind', 'is_device')
            for key in extra_keys:
                status_info.pop(key)
            for key, value in status_info.items():
                if isinstance(value, dict):
                    # Go recursive
                    inner = self._status_info_lines(value,
                                                    prefix=full_name + '_',
                                                    indent=2)
                    data_lines.extend(inner)
                else:
                    # Record extra value
                    data_lines.append(f'{key}: {value}')
            if data_lines:
                # Indent the subdevices
                if indent:
                    for i, line in enumerate(data_lines):
                        data_lines[i] = ' ' * indent + line
                return header_lines + data_lines
            else:
                # No data = do not print header
                return []
        else:
            # Show the name/value pair for a signal
            value = status_info['value']
            units = status_info.get('units') or ''
            if units:
                units = f' [{units}]'
            value_text = str(value)
            if '\n' in value_text:
                # Multiline values (arrays) need special handling
                value_lines = value_text.split('\n')
                for i, line in enumerate(value_lines):
                    value_lines[i] = ' ' * 2 + line
                return [f'{name}:'] + value_lines
            else:
                return [f'{name}: {value}{units}']

    def status_info(self):
        """
        Get useful information for the status display.

        This can be overridden if a device wants to feed custom information to
        the formatter.

        Returns
        -------
        info: dict
            Nested dictionary. Each level has keys name, kind, and is_device.
            If is_device is True, subdevice dictionaries may follow. Otherwise,
            the only other key in the dictionary will be value.
        """
        def subdevice_filter(info):
            return bool(info['kind'] & Kind.normal)

        return ophydobj_info(self, subdevice_filter=subdevice_filter)


def get_name(obj, default):
    try:
        return obj.name
    except AttributeError:
        try:
            return str(obj)
        except Exception:
            return default


def get_kind(obj):
    try:
        return obj.kind
    except Exception:
        return Kind.omitted


def get_value(signal):
    try:
        # Minimize waiting, we aren't collecting data we're showing info
        if signal.connected:
            return signal.get(timeout=0.1, connection_timeout=0.1)
    except Exception:
        pass
    return None


def get_units(signal):
    attrs = ('units', 'egu', 'derived_units')
    for attr in attrs:
        try:
            value = getattr(signal, attr, None) or signal.metadata[attr]
            if isinstance(value, str):
                return value
        except Exception:
            ...


def ophydobj_info(obj, subdevice_filter=None, devices=None):
    if isinstance(obj, Signal):
        return signal_info(obj)
    elif isinstance(obj, Device):
        return device_info(obj, subdevice_filter=subdevice_filter,
                           devices=devices)
    else:
        return {}


def device_info(device, subdevice_filter=None, devices=None):
    if devices is None:
        devices = set()
    name = get_name(device, default='device')
    kind = get_kind(device)
    info = dict(name=name, kind=kind, is_device=True)

    try:
        # Show the current preset state if we have one
        # This should be the first key in the ordered dict
        has_presets = device.presets.has_presets
    except AttributeError:
        has_presets = False
    if has_presets:
        try:
            info['preset'] = device.presets.state()
        except Exception:
            info['preset'] = 'ERROR'

    try:
        # Extra key for positioners
        # This has ordered dict priority over everything but the preset state
        info['position'] = device.position
    except AttributeError:
        pass
    except Exception:
        # Something else went wrong! We have a position but it didn't work
        info['position'] = 'ERROR'

    if device not in devices:
        devices.add(device)
        for cpt_name, cpt_desc in device._sig_attrs.items():
            # Skip lazy signals outright in all cases
            # Usually these are lazy because they take too long to getattr
            if cpt_desc.lazy:
                continue
            # Skip attribute signals
            # Indeterminate get times, no real connected bool, etc.
            if issubclass(cpt_desc.cls, AttributeSignal):
                continue
            # Skip not implemented signals
            # They never have interesting information
            if issubclass(cpt_desc.cls, NotImplementedSignal):
                continue
            try:
                cpt = getattr(device, cpt_name)
            except AttributeError:
                # Why are we ever in this block?
                logger.debug(f'Getattr {name}.{cpt_name} failed.',
                             exc_info=True)
                continue
            cpt_info = ophydobj_info(cpt, subdevice_filter=subdevice_filter,
                                     devices=devices)
            if 'position' in info:
                # Drop some potential duplicate keys for positioners
                try:
                    if cpt.name == cpt.parent.name:
                        continue
                except AttributeError:
                    pass
                if cpt_name in ('readback', 'user_readback'):
                    continue

            if not callable(subdevice_filter) or subdevice_filter(cpt_info):
                info[cpt_name] = cpt_info
    return info


def signal_info(signal):
    name = get_name(signal, default='signal')
    kind = get_kind(signal)
    value = get_value(signal)
    units = get_units(signal)
    return dict(name=name, kind=kind, is_device=False, value=value,
                units=units)


def set_engineering_mode(expert):
    """
    Switches between expert and user modes for :class:`BaseInterface` features.

    Current features:
       - Autocomplete filtering

    Parameters
    ----------
    expert : bool
        Set to `True` to enable expert mode, or :keyword:`False` to
        disable it. `True` is the starting value.
    """

    global engineering_mode
    engineering_mode = bool(expert)


def get_engineering_mode():
    """
    Get the last value set by :meth:`set_engineering_mode`.

    Returns
    -------
    expert : bool
        The current engineering mode. See :meth:`set_engineering_mode`.
    """

    return engineering_mode


class MvInterface(BaseInterface):
    """
    Interface layer to attach to a positioner for motion shortcuts.

    Defines common shortcuts that the beamline scientists like for moving
    things on the command line. There is no need for these in a scripting
    environnment, but this is a safe space for implementing move features that
    would otherwise be disruptive to running scans and writing higher-level
    applications.
    """

    tab_whitelist = ["mv", "wm", "camonitor", "wm_update"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mov_ev = Event()

    def _log_move(self, position):
        logger.info('Moving %s from %s to %s', self.name, self.wm(), position)

    def mv(self, position, timeout=None, wait=False):
        """
        Absolute move to a position.

        Parameters
        ----------
        position
            Desired end position.

        timeout : float, optional
            If provided, the mover will throw an error if motion takes longer
            than timeout to complete. If omitted, the mover's default timeout
            will be use.

        wait : bool, optional
            If `True`, wait for motion completion before returning.
            Defaults to :keyword:`False`.
        """
        self._log_move(position)
        self.move(position, timeout=timeout, wait=wait)

    def wm(self):
        """Get the mover's current positon (where motor)."""
        return self.position

    def __call__(self, position=None, timeout=None, wait=False):
        """
        Dispatches to :meth:`mv` or :meth:`wm` based on the arguments.

        Calling the object will either move the object or get the current
        position, depending on if the position argument is given. See the
        docstrings for :meth:`mv` and :meth:`wm`.
        """

        if position is None:
            return self.wm()
        else:
            self.mv(position, timeout=timeout, wait=wait)

    def camonitor(self):
        """
        Shows a live-updating motor position in the terminal.

        This will be the value that is returned by the :attr:`position`
        attribute.

        This method ends cleanly at a ctrl+c or after a call to
        :meth:`end_monitor_thread`, which may be useful when this is called in
        a background thread.
        """

        try:
            self._mov_ev.clear()
            while not self._mov_ev.is_set():
                print("\r {0:4f}".format(self.wm()), end=" ")
                self._mov_ev.wait(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            self._mov_ev.clear()

    # Legacy alias
    def wm_update(self):
        return self.camonitor()

    wm_update.__doc__ = camonitor.__doc__

    def end_monitor_thread(self):
        """
        Stop a :meth:`camonitor` or :meth:`wm_update` that is running in
        another thread.
        """
        self._mov_ev.set()


class FltMvInterface(MvInterface):
    """
    Extension of :class:`MvInterface` for when the position is a float.

    This lets us do more with the interface, such as relative moves.

    Attributes
    ----------
    presets : :class:`Presets`
        Manager for preset positions.
    """

    tab_whitelist = ["mvr", "umv", "umvr", "mv_ginput", "tweak",
                     "presets", "mv_.*", "wm_.*", "umv_.*"]

    @property
    def presets(self):
        if not hasattr(self, "_presets"):
            self._presets = Presets(self)
        return self._presets

    def mvr(self, delta, timeout=None, wait=False):
        """
        Relative move from this position.

        Parameters
        ----------
        delta : float
            Desired change in position.

        timeout : float, optional
            If provided, the mover will throw an error if motion takes longer
            than timeout to complete. If omitted, the mover's default timeout
            will be use.

        wait : bool, optional
            If `True`, wait for motion completion before returning.
            Defaults to :keyword:`False`.
        """

        self.mv(delta + self.wm(), timeout=timeout, wait=wait)

    def umv(self, position, timeout=None):
        """
        Move to a position, wait, and update with a progress bar.

        Parameters
        ----------
        position : float
            Desired end position.

        timeout : float, optional
            If provided, the mover will throw an error if motion takes longer
            than timeout to complete. If omitted, the mover's default timeout
            will be use.
        """

        self._log_move(position)
        status = self.move(position, timeout=timeout, wait=False)
        pgb = AbsProgressBar([status])
        try:
            status_wait(status)
        except KeyboardInterrupt:
            self.stop()
            pgb.no_more_updates()
        print()

    def umvr(self, delta, timeout=None):
        """
        Relative move from this position, wait, and update with a progress bar.

        Parameters
        ----------
        delta : float
            Desired change in position.

        timeout : float, optional
            If provided, the mover will throw an error if motion takes longer
            than timeout to complete. If omitted, the mover's default timeout
            will be use.
        """

        self.umv(delta + self.wm(), timeout=timeout)

    def mv_ginput(self, timeout=None):
        """
        Moves to a location the user clicks on.

        If there are existing plots, this will be the position on the most
        recently active plot. If there are no existing plots, an empty plot
        will be created with the motor's limits as the range.
        """

        # Importing forces backend selection, so do inside method
        import matplotlib.pyplot as plt  # NOQA
        logger.info(("Select new motor x-position in current plot "
                     "by mouseclick"))
        if not plt.get_fignums():
            upper_limit = 0
            lower_limit = self.limits[0]
            if self.limits[0] == self.limits[1]:
                upper_limit = self.limits[0]+100
            else:
                upper_limit = self.limits[1]
            limit_plot = []
            for x in range(lower_limit, upper_limit):
                limit_plot.append(x)
            plt.plot(limit_plot)
        pos = plt.ginput(1)[0][0]
        self.move(pos, timeout=timeout)

    def tweak(self):
        """
        Control this motor using the arrow keys.

        Use left arrow to step negative and right arrow to step positive.
        Use up arrow to increase step size and down arrow to decrease step
        size. Press q or ctrl+c to quit.
        """

        return tweak_base(self)


def setup_preset_paths(**paths):
    """
    Prepare the :class:`Presets` class.

    Sets the paths for saving and loading presets.

    Parameters
    ----------
    **paths : str keyword args
        A mapping from type of preset to destination path. These will be
        directories that contain the yaml files that define the preset
        positions.
    """

    Presets._paths = {}
    for k, v in paths.items():
        Presets._paths[k] = Path(v)
    for preset in Presets._registry:
        preset.sync()


class Presets:
    """
    Manager for device preset positions.

    This provides methods for adding new presets, checking which presets are
    active, and related utilities.

    It will install the :meth:`mv_presetname` and :meth:`wm_presetname` methods
    onto the associated device, and the :meth:`add_preset` and
    :meth:`add_preset_here` methods onto itself.

    Parameters
    ----------
    device : :class:`~ophyd.device.Device`
        The device to manage saved preset positions for. It must implement the
        :class:`FltMvInterface`.

    Attributes
    ----------
    positions : :class:`~types.SimpleNamespace`
        A namespace that contains all of the active presets as
        :class:`PresetPosition` objects.
    """

    _registry = WeakSet()
    _paths = {}

    def __init__(self, device):
        self._device = device
        self._methods = []
        self._fd = None
        self._registry.add(self)
        self.name = device.name + '_presets'
        self.sync()

    def _path(self, preset_type):
        """Utility function to get the preset file :class:`~pathlib.Path`."""
        path = self._paths[preset_type] / (self._device.name + '.yml')
        logger.debug('select presets path %s', path)
        return path

    def _read(self, preset_type):
        """Utility function to get a particular preset's datum dictionary."""
        logger.debug('read presets for %s', self._device.name)
        with self._file_open_rlock(preset_type) as f:
            f.seek(0)
            return yaml.full_load(f) or {}

    def _write(self, preset_type, data):
        """
        Utility function to overwrite a particular preset's datum dictionary.
        """
        logger.debug('write presets for %s', self._device.name)
        with self._file_open_rlock(preset_type) as f:
            f.seek(0)
            yaml.dump(data, f, default_flow_style=False)
            f.truncate()

    @contextmanager
    def _file_open_rlock(self, preset_type, timeout=1.0):
        """
        File locking context manager for this object.

        Works like threading.Rlock in that you can acquire it multiple times
        safely.

        Parameters
        ----------
        fd : file
            The file descriptor to lock on.

        Raises
        ------
        BlockingIOError
            If we cannot acquire the file lock.
        """

        if self._fd is None:
            path = self._path(preset_type)
            with open(path, 'r+') as fd:
                # Set up file lock timeout with a raising handler
                # We will need this handler due to PEP 475
                def interrupt(signum, frame):
                    raise InterruptedError()

                old_handler = signal.signal(signal.SIGALRM, interrupt)
                try:
                    signal.setitimer(signal.ITIMER_REAL, timeout)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except InterruptedError:
                    # Ignore interrupted and proceed to cleanup
                    pass
                finally:
                    # Clean up file lock timeout
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, old_handler)
                # Error now if we still can't get the lock.
                # Getting lock twice is safe.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                logger.debug('acquired lock for %s', path)
                self._fd = fd
                yield fd
                fcntl.flock(fd, fcntl.LOCK_UN)
                logger.debug('released lock for %s', path)
            self._fd = None
        else:
            logger.debug('using already open file descriptor')
            yield self._fd

    def _update(self, preset_type, name, value=None, comment=None,
                active=True):
        """
        Utility function to update a preset position.

        Reads the existing preset's datum, updates the value the comment, and
        the active state, and then writes the datum back to the file, updating
        the history accordingly.
        """

        logger.debug(('call %s presets._update(%s, %s, value=%s, comment=%s, '
                      'active=%s)'), self._device.name, preset_type, name,
                     value, comment, active)
        if not isinstance(name, str):
            raise TypeError(('name must be of type <str>, not type'
                             '{}'.format(type(name))))
        if value is not None and not isinstance(value, numbers.Real):
            raise TypeError(('value must be a real numeric type, not type'
                             '{}'.format(type(value))))
        try:
            path = self._path(preset_type)
            if not path.exists():
                path.touch()
                path.chmod(0o666)
            with self._file_open_rlock(preset_type):
                data = self._read(preset_type)
                if value is None and comment is not None:
                    value = data[name]['value']
                if value is not None:
                    if name not in data:
                        data[name] = {}
                    ts = time.strftime('%d %b %Y %H:%M:%S')
                    data[name]['value'] = value
                    history = data[name].get('history', {})
                    if comment:
                        comment = ' ' + comment
                    else:
                        comment = ''
                    history[ts] = '{:10.4f}{}'.format(value, comment)
                    data[name]['history'] = history
                if active:
                    data[name]['active'] = True
                else:
                    data[name]['active'] = False
                self._write(preset_type, data)
        except BlockingIOError:
            self._log_flock_error()

    def sync(self):
        """Synchronize the presets with the database."""
        logger.debug('call %s presets.sync()', self._device.name)
        self._remove_methods()
        self._cache = {}
        logger.debug('filling %s cache', self.name)
        for preset_type in self._paths.keys():
            path = self._path(preset_type)
            if path.exists():
                try:
                    self._cache[preset_type] = self._read(preset_type)
                except BlockingIOError:
                    self._log_flock_error()
            else:
                logger.debug('No %s preset file for %s',
                             preset_type, self._device.name)
        self._create_methods()

    def _log_flock_error(self):
        logger.error(('Unable to acquire file lock for %s. '
                      'File may be being edited by another user.'), self.name)
        logger.debug('', exc_info=True)

    def _create_methods(self):
        """
        Create the dynamic methods based on the configured paths.

        Add methods to this object for adding presets of each type, add
        methods to the associated device to move and check each preset, and
        add :class:`PresetPosition` instances to :attr:`.positions` for
        each preset name.
        """

        logger.debug('call %s presets._create_methods()', self._device.name)
        for preset_type in self._paths.keys():
            add, add_here = self._make_add(preset_type)
            self._register_method(self, 'add_' + preset_type, add)
            self._register_method(self, 'add_here_' + preset_type, add_here)
        for preset_type, data in self._cache.items():
            for name, info in data.items():
                if info['active']:
                    mv, umv = self._make_mv_pre(preset_type, name)
                    wm = self._make_wm_pre(preset_type, name)
                    self._register_method(self._device, 'mv_' + name, mv)
                    self._register_method(self._device, 'umv_' + name, umv)
                    self._register_method(self._device, 'wm_' + name, wm)
                    setattr(self.positions, name,
                            PresetPosition(self, preset_type, name))

    def _register_method(self, obj, method_name, method):
        """
        Utility function for managing dynamic methods.

        Adds a method to the :attr:`._methods` list and binds the method to an
        object.
        """

        logger.debug('register method %s to %s', method_name, obj.name)
        self._methods.append((obj, method_name))
        setattr(obj, method_name, MethodType(method, obj))

    def _make_add(self, preset_type):
        """
        Create the functions that add preset positions.

        Creates suitable versions of :meth:`.add` and :meth:`.add_here` for a
        particular preset type, e.g. ``add_preset_type`` and
        ``add_here_preset_type``.
        """

        def add(self, name, value, comment=None):
            """
            Add a preset position of type "{}".

            Parameters
            ----------
            name : str
                The name of the new preset position.

            value : float
                The value of the new preset_position.

            comment : str, optional
                A comment to associate with the preset position.
            """

            self._update(preset_type, name, value=value,
                         comment=comment)
            self.sync()

        def add_here(self, name, comment=None):
            """
            Add a preset of the current position of type "{}".

            Parameters
            ----------
            name : str
                The name of the new preset position.

            comment : str, optional
                A comment to associate with the preset position.
            """

            add(self, name, self._device.wm(), comment=comment)

        add.__doc__ = add.__doc__.format(preset_type)
        add_here.__doc__ = add_here.__doc__.format(preset_type)
        return add, add_here

    def _make_mv_pre(self, preset_type, name):
        """
        Create the functions that move to preset positions.

        Creates a suitable versions of :meth:`~MvInterface.mv` and
        :meth:`~MvInterface.umv` for a particular preset type and name
        e.g. ``mv_sample``.
        """

        def mv_pre(self, timeout=None, wait=False):
            """
            Move to the {} preset position.

            Parameters
            ----------
            timeout : float, optional
                If provided, the mover will throw an error if motion takes
                longer than timeout to complete. If omitted, the mover's
                default timeout will be use.

            wait : bool, optional
                If `True`, wait for motion completion before
                returning. Defaults to :keyword:`False`.
            """

            pos = self.presets._cache[preset_type][name]['value']
            self.mv(pos, timeout=timeout, wait=wait)

        def umv_pre(self, timeout=None):
            """
            Update move to the {} preset position.

            Parameters
            ----------
            timeout : float, optional
                If provided, the mover will throw an error if motion takes
                longer than timeout to complete. If omitted, the mover's
                default timeout will be use.
            """

            pos = self.presets._cache[preset_type][name]['value']
            self.umv(pos, timeout=timeout)

        mv_pre.__doc__ = mv_pre.__doc__.format(name)
        umv_pre.__doc__ = umv_pre.__doc__.format(name)
        return mv_pre, umv_pre

    def _make_wm_pre(self, preset_type, name):
        """
        Create a method to get the offset from a preset position.

        Creates a suitable version of :meth:`~MvInterface.wm` for a particular
        preset type and name e.g. ``wm_sample``.
        """

        def wm_pre(self):
            """
            Check the offset from the {} preset position.

            Returns
            -------
            offset : float
                How far we are from the preset position. If this is near zero,
                we are at the position. If this positive, the preset position
                is in the positive direction from us.
            """

            pos = self.presets._cache[preset_type][name]['value']
            return pos - self.wm()

        wm_pre.__doc__ = wm_pre.__doc__.format(name)
        return wm_pre

    def _remove_methods(self):
        """Remove all methods created in the last call to _create_methods."""
        logger.debug('call %s presets._remove_methods()', self._device.name)
        for obj, method_name in self._methods:
            try:
                delattr(obj, method_name)
            except AttributeError:
                pass
        self._methods = []
        self.positions = SimpleNamespace()

    @property
    def has_presets(self):
        """
        Returns True if any preset positions are defined.
        """
        return bool(self.positions.__dict__)

    def state(self):
        """
        Return the current active preset state name.

        This will be the state string name, or Unknown if we're not at any
        state.
        """
        state = 'Unknown'
        closest = 0.5
        for device, method_name in self._methods:
            if method_name.startswith('wm_'):
                state_name = method_name.replace('wm_', '', 1)
                wm_state = getattr(device, method_name)
                diff = wm_state()
                if diff < closest:
                    state = state_name
                    closest = diff
        return state


class PresetPosition:
    """
    Manager for a single preset position.

    Parameters
    ----------
    presets : :class:`Presets`
        The main :class:`Presets` object that manages this position.

    name : str
        The name of this preset position.
    """

    def __init__(self, presets, preset_type, name):
        self._presets = presets
        self._preset_type = preset_type
        self._name = name

    def update_pos(self, pos=None, comment=None):
        """
        Change this preset position and save it.

        Parameters
        ----------
        pos : float, optional
            The position to use for this preset. If omitted, we'll use the
            current position.

        comment : str, optional
            A comment to associate with the preset position.
        """

        if pos is None:
            pos = self._presets._device.wm()
        self._presets._update(self._preset_type, self._name, value=pos,
                              comment=comment)
        self._presets.sync()

    def update_comment(self, comment):
        """
        Revise the most recent comment in the preset history.

        Parameters
        ----------
        comment : str
            A comment to associate with the preset position.
        """

        self._presets._update(self._preset_type, self._name, comment=comment)
        self._presets.sync()

    def deactivate(self):
        """
        Deactivate a preset from a device.
        This can always be undone unless you edit the underlying file.
        """
        self._presets._update(self._preset_type, self._name, active=False)
        self._presets.sync()

    @property
    def info(self):
        """All information associated with this preset, returned as a dict."""
        return self._presets._cache[self._preset_type][self._name]

    @property
    def pos(self):
        """The set position of this preset, returned as a float."""
        return self.info['value']

    @property
    def history(self):
        """
        This position history associated with this preset, returned as a dict.
        """
        return self.info['history']

    @property
    def path(self):
        """The filepath that defines this preset, returned as a string."""
        return str(self._presets._path(self._preset_type))

    def __repr__(self):
        return str(self.pos)


def tweak_base(*args):
    """
    Base function to control motors with the arrow keys.

    With one motor, this will use the left and right arrows for the axis and up
    and down arrows for scaling the step size. With two motors, this will use
    left and right for the first axis and up and down for the second axis, with
    shift+arrow used for scaling the step size. The q key quits, as does
    ctrl+c.
    """

    up = util.arrow_up
    down = util.arrow_down
    left = util.arrow_left
    right = util.arrow_right
    shift_up = util.shift_arrow_up
    shift_down = util.shift_arrow_down
    scale = 0.1

    def thread_event():
        """Function call camonitor to display motor position."""
        thrd = Thread(target=args[0].camonitor,)
        thrd.start()
        args[0]._mov_ev.set()

    def _scale(scale, direction):
        """Function used to change the scale."""
        if direction == up or direction == shift_up:
            scale = scale*2
            print("\r {0:4f}".format(scale), end=" ")
        elif direction == down or direction == shift_down:
            scale = scale/2
            print("\r {0:4f}".format(scale), end=" ")
        return scale

    def movement(scale, direction):
        """Function used to know when and the direction to move the motor."""
        try:
            if direction == left:
                args[0].umvr(-scale)
                thread_event()
            elif direction == right:
                args[0].umvr(scale)
                thread_event()
            elif direction == up and len(args) > 1:
                args[1].umvr(scale)
                print("\r {0:4f}".format(args[1].position), end=" ")
        except Exception as exc:
            logger.error('Error in tweak move: %s', exc)
            logger.debug('', exc_info=True)

    # Loop takes in user key input and stops when 'q' is pressed
    if len(args) == 1:
        logger.info('Started tweak of %s', args[0])
    else:
        logger.info('Started tweak of %s', [mot.name for mot in args])
    is_input = True
    while is_input is True:
        inp = util.get_input()
        if inp in ('q', None):
            is_input = False
        else:
            if len(args) > 1 and inp == down:
                movement(-scale, up)
            elif len(args) > 1 and inp == up:
                movement(scale, inp)
            elif inp not in (up, down, left, right, shift_down, shift_up):
                print()  # Newline
                if len(args) == 1:
                    print(" Left: move x motor backward")
                    print(" Right: move x motor forward")
                    print(" Up: scale*2")
                    print(" Down: scale/2")
                else:
                    print(" Left: move x motor left")
                    print(" Right: move x motor right")
                    print(" Down: move y motor down")
                    print(" Up: move y motor up")
                    print(" Shift_Up: scale*2")
                    print(" Shift_Down: scale/2")
                print(" Press q to quit."
                      " Press any other key to display this message.")
                print()  # Newline
            else:
                movement(scale, inp)
                scale = _scale(scale, inp)
    print()


class AbsProgressBar(ProgressBar):
    """Progress bar that displays the absolute position as well."""
    def __init__(self, status_objs, **kwargs):
        self._last_position = None
        self._name = None
        self._no_more = False
        super().__init__(status_objs, **kwargs)

        # Extra call when status is complete
        for i, obj in enumerate(status_objs):
            obj.add_callback(functools.partial(self._status_cb, i))

    def _status_cb(self, pos, status):
        self.update(pos, name=self._name, current=self._last_position)

    def update(self, *args, name=None, current=None, **kwargs):
        if self._no_more:
            return
        current = current or self._last_position
        self._name = self._name or name

        try:
            name = '{} ({:.3f})'.format(name, current)
            self._last_position = current
        except Exception:
            name = name or self._name or 'motor'

        super().update(*args, name=name, current=current, **kwargs)

    def no_more_updates(self):
        self._no_more = True


class LightpathMixin(OphydObject):
    """
    Mix-in class that makes it easier to establish a lightpath interface.

    Use this on classes that are not state positioners but would still like to
    be used as a top-level device in lightpath.
    """
    SUB_STATE = 'state'
    _default_sub = SUB_STATE

    # Component names whose values are relevant for inserted/removed
    lightpath_cpts = []

    # Flag to signify that subclass is another mixin, rather than a device
    _lightpath_mixin = False

    def __init__(self, *args, **kwargs):
        self._lightpath_values = {}
        self._lightpath_ready = False
        self._retry_lightpath = False
        super().__init__(*args, **kwargs)

    def __init_subclass__(cls, **kwargs):
        # Magic to subscribe to the list of components
        super().__init_subclass__(**kwargs)
        if cls._lightpath_mixin:
            # Child of cls will inherit this as False
            cls._lightpath_mixin = False
        else:
            if not cls.lightpath_cpts:
                raise NotImplementedError('Did not implement LightpathMixin')
            for cpt_name in cls.lightpath_cpts:
                cpt = getattr(cls, cpt_name)
                cpt.sub_default(cls._update_lightpath)

    def _set_lightpath_states(self, lightpath_values):
        # Override based on the use case
        # update self._inserted, self._removed,
        # and optionally self._transmission
        # Should return a dict or None
        raise NotImplementedError('Did not implement LightpathMixin')

    def _update_lightpath(self, *args, obj, **kwargs):
        try:
            # Universally cache values
            self._lightpath_values[obj] = kwargs
            # Only do the first lightpath state once all cpts have chimed in
            if len(self._lightpath_values) >= len(self.lightpath_cpts):
                self._retry_lightpath = False
                # Pass user function the full set of values
                self._set_lightpath_states(self._lightpath_values)
                self._lightpath_ready = not self._retry_lightpath
                if self._lightpath_ready:
                    # Tell lightpath to update
                    self._run_subs(sub_type=self.SUB_STATE)
                elif self._retry_lightpath and not self._destroyed:
                    # Use this when the device wasn't ready to set states
                    kw = dict(obj=obj)
                    kw.update(kwargs)
                    util.schedule_task(self._update_lightpath,
                                       args=args, kwargs=kw, delay=0.2)
        except Exception:
            # Without this, callbacks fail silently
            logger.exception('Error in lightpath update callback for %s.',
                             self.name)

    @property
    def inserted(self):
        return self._lightpath_ready and bool(self._inserted)

    @property
    def removed(self):
        return self._lightpath_ready and bool(self._removed)

    @property
    def transmission(self):
        try:
            return self._transmission
        except AttributeError:
            if self.inserted:
                return 0
            else:
                return 1


class LightpathInOutMixin(LightpathMixin):
    """
    LightpathMixin for parent device with InOut subdevices.
    Also works recursively on other LightpathInOutMixin subclasses.
    """
    _lightpath_mixin = True

    def _set_lightpath_states(self, lightpath_values):
        in_check = []
        out_check = []
        trans_check = []
        for obj, kwarg_dct in lightpath_values.items():
            if isinstance(obj, LightpathInOutMixin):
                # The inserted/removed are always just a getattr
                # Therefore, they are safe to call in a callback
                in_check.append(obj.inserted)
                out_check.append(obj.removed)
                trans_check.append(obj.transmission)
            else:
                if not obj._state_initialized:
                    # This would prevent make check_inserted, etc. fail
                    self._retry_lightpath = True
                    return
                # Inserted/removed are not getattr, they can check EPICS
                # Instead, check status against the callback kwarg dict
                in_check.append(obj.check_inserted(kwarg_dct['value']))
                out_check.append(obj.check_removed(kwarg_dct['value']))
                trans_check.append(obj.check_transmission(kwarg_dct['value']))
        self._inserted = any(in_check)
        self._removed = all(out_check)
        self._transmission = functools.reduce(lambda a, b: a*b, trans_check)
        return dict(in_check=in_check, out_check=out_check,
                    trans_check=trans_check)
