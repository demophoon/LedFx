import asyncio
import concurrent.futures
import importlib
import inspect
import ipaddress
import logging
import os
import pkgutil
import re
import socket
import sys
from abc import ABC

# from asyncio import coroutines, ensure_future
from subprocess import PIPE, Popen

import requests
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)


def install_package(package):
    _LOGGER.info(f"Installed package: {package}")
    env = os.environ.copy()
    args = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        package,
    ]
    process = Popen(args, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env)
    _, stderr = process.communicate()
    if process.returncode != 0:
        _LOGGER.error(
            "Failed to install package %s: %s",
            package,
            stderr.decode("utf-8").lstrip().strip(),
        )
        return False
    return True


def import_or_install(package):
    try:
        _LOGGER.info(f"Imported package: {package}")
        return importlib.import_module(package)

    except ImportError:
        install_package(package)
        try:
            return importlib.import_module(package)
        except ImportError:
            return False
    return False


def async_fire_and_forget(coro, loop):
    """Run some code in the core event loop without a result"""

    if not asyncio.coroutines.iscoroutine(coro):
        raise TypeError(("A coroutine object is required: {}").format(coro))

    def callback():
        """Handle the firing of a coroutine."""
        asyncio.ensure_future(coro, loop=loop)

    loop.call_soon_threadsafe(callback)
    return


def async_fire_and_return(coro, loop, timeout=10):
    """Run some code in the core event loop with a result"""

    if not asyncio.coroutines.iscoroutine(coro):
        raise TypeError(("A coroutine object is required: {}").format(coro))

    def callback(result):
        print("Callback!")
        print(result.result())

    future = asyncio.ensure_future(coro, loop=loop)
    future.add_done_callback(callback)

    try:
        result = future.result(timeout)
    except asyncio.TimeoutError:
        _LOGGER.warning(
            f"Coroutine {coro} timed out at {timeout}s, cancelling the task..."
        )
        future.cancel()
    except Exception as exc:
        _LOGGER.error(f"Coroutine {coro} raised an exception: {exc!r}")
    else:
        return result


def async_callback(loop, callback, *args):
    """Run a callback in the event loop with access to the result"""

    future = concurrent.futures.Future()

    def run_callback():
        try:
            future.set_result(callback(*args))
        # pylint: disable=broad-except
        except Exception as e:
            if future.set_running_or_notify_cancel():
                future.set_exception(e)
            else:
                _LOGGER.warning("Exception on lost future: ", exc_info=True)

    loop.call_soon_threadsafe(run_callback)
    return future


class WLED:
    """
    A collection of WLED helper functions
    These are currently blocking, syncronous calls
    Should make them async in future
    """

    SYNC_MODES = {"ddp": 4048, "e131": 5568, "artnet": 6454}

    def _wled_request(
        self, method, ip_address, endpoint, timeout=0.5, **kwargs
    ):
        url = f"http://{ip_address}/{endpoint}"

        try:
            response = method(url, timeout=timeout, **kwargs)

        except requests.exceptions.RequestException:
            msg = f"Cannot connect to WLED device at {ip_address}"
            raise ValueError(msg)

        if not response.ok:
            msg = f"WLED API Error at {ip_address}: {response.status_code}"
            raise ValueError(msg)

        return response

    def get_config(self, ip_address):
        """
            Uses a JSON API call to determine if the device is WLED or WLED compatible
            and return its config.
            Specifically searches for "WLED" in the brand json - currently all major
            branches/forks of WLED contain WLED in the branch data.
        Args:
            ip_address (String): the IP to query
        Returns:
            config: dict, with all wled configuration info
        """
        response = self._wled_request(requests.get, ip_address, "json/info")

        wled_config = response.json()

        if not wled_config["brand"] in "WLED":
            msg = f"{ip_address} is not WLED compatible, brand: {wled_config['brand']}"
            raise ValueError(msg)

        return wled_config

    def get_state(self, ip_address):
        """
            Uses a JSON API call to determine the full WLED device state

        Args:
            ip_address (string): The device IP to be queried
        Returns:
            state, dict. Full device state
        """
        response = self._wled_request(requests.get, ip_address, "json/state")

        return response.json()

    def get_power_state(self, ip_address):
        """
            Uses a JSON API call to determine the WLED device power state (on/off)

        Args:
            ip_address (string): The device IP to be queried
        Returns:
            boolean: True is "On", False is "Off"
        """
        return self.get_state(ip_address)["on"]

    def get_segments(self, ip_address):
        """
            Uses a JSON API call to determine the WLED segment setup

        Args:
            ip_address (string): The device IP to be queried
        Returns:
            dict: array of segments
        """
        return self.get_state(ip_address)["seg"]

    def set_power_state(self, ip_address, state):
        """
            Uses a HTTP post call to set the power of a WLED compatible device on/off

        Args:
            ip_address (string): The device IP to be turned on
            state (bool): on/off
        """
        self._wled_request(
            requests.post, ip_address, f"win&T={'1' if state else '0'}"
        )

        _LOGGER.info(
            f"Turned WLED device at {ip_address} {'on' if state else 'off'}."
        )

    def set_brightness(self, ip_address, brightness):
        """
            Uses a HTTP post call to adjust a WLED compatible device's
            brightness

        Args:
            ip_address (string): The device IP to adjust brightness
            brightness (int): The brightness value between 0-255
        """
        # cast to int and clamp to range
        brightness = max(0, max(int(brightness), 255))

        self._wled_request(requests.post, ip_address, f"win&A={brightness}")

        _LOGGER.info(
            f"Set WLED device brightness at {ip_address} to {brightness}."
        )

    def set_sync_mode(self, ip_address, mode):
        """
            Uses a HTTP post call to set a WLED compatible device's
            sync mode

        Args:
            ip_address (string): The device IP to adjust brightness
            mode: str, in ["ddp", "e131", "artnet"]
        """
        port = self.SYNC_MODES["mode"]

        self._wled_request(
            requests.post,
            ip_address,
            "settings/sync",
            data={"DI": port, "EP": port},
        )

        _LOGGER.info(f"Set WLED device at {ip_address} to sync mode '{mode}'")


def resolve_destination(destination):
    """Uses a socket to attempt domain lookup

    Args:
        destination (string): The domain name to be resolved.

    Returns:
        On success: string containing the resolved IP address.
        On failure: boolean false.
    """
    try:
        ipaddress.ip_address(destination)
        return destination
    except ValueError:

        cleaned_dest = destination.rstrip(".")

        try:
            return socket.gethostbyname(cleaned_dest)
        except socket.gaierror:
            _LOGGER.warning(f"Failed resolving {cleaned_dest}.")
        return False


def currently_frozen():
    """Checks to see if running in a frozen environment such as pyinstaller or pyupdater package
    Args:
        Nil

    Returns:
        boolean
    """
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def generate_id(name):
    """Converts a name to a id"""
    part1 = re.sub("[^a-zA-Z0-9]", " ", name).lower()
    return re.sub(" +", " ", part1).strip().replace(" ", "-")


def generate_title(id):
    """Converts an id to a more human readable title"""
    return re.sub("[^a-zA-Z0-9]", " ", id).title()


def hasattr_explicit(cls, attr):
    """Returns if the given object has explicitly declared an attribute"""
    try:
        return getattr(cls, attr) != getattr(super(cls, cls), attr, None)
    except AttributeError:
        return False


def getattr_explicit(cls, attr, *default):
    """Gets an explicit attribute from an object"""

    if len(default) > 1:
        raise TypeError(
            "getattr_explicit expected at most 3 arguments, got {}".format(
                len(default) + 2
            )
        )

    if hasattr_explicit(cls, attr):
        return getattr(cls, attr, default)
    if default:
        return default[0]

    raise AttributeError(
        "type object '{}' has no attribute '{}'.".format(cls.__name__, attr)
    )


class RollingQueueHandler(logging.handlers.QueueHandler):
    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self.queue.get_nowait()
            self.enqueue(record)


class BaseRegistry(ABC):
    """
    Base registry class used for effects and devices. This maintains a
    list of automatically registered base classes and assembles schema
    information

    The prevent registration for classes that are intended to serve as
    base classes (i.e. GradientEffect) add the following declarator:
        @Effect.no_registration
    """

    _schema_attr = "CONFIG_SCHEMA"

    def __init_subclass__(cls, **kwargs):
        """Automatically register the class"""
        super().__init_subclass__(**kwargs)

        if not hasattr(cls, "_registry"):
            cls._registry = {}

        name = cls.__module__.split(".")[-1]
        cls._registry[name] = cls

    @classmethod
    def no_registration(self, cls):
        """Clear registration entity based on special declarator"""

        name = cls.__module__.split(".")[-1]
        del cls._registry[name]
        return cls

    @classmethod
    def schema(self, extended=True, extra=vol.ALLOW_EXTRA):
        """Returns the extended schema of the class"""

        if extended is False:
            return getattr_explicit(
                type(self), self._schema_attr, vol.Schema({})
            )

        schema = vol.Schema({}, extra=extra)
        classes = inspect.getmro(self)[::-1]
        for c in classes:
            c_schema = getattr_explicit(c, self._schema_attr, None)
            if c_schema is not None:
                schema = schema.extend(c_schema.schema)

        return schema

    @classmethod
    def registry(self):
        """Returns all the subclasses in the registry"""

        return self._registry

    @property
    def id(self) -> str:
        """Returns the id for the object"""
        return getattr(self, "_id", None)

    @property
    def type(self) -> str:
        """Returns the type for the object"""
        return getattr(self, "_type", None)

    @property
    def config(self) -> dict:
        """Returns the config for the object"""
        return getattr(self, "_config", None)

    @config.setter
    def config(self, _config):
        """Updates the config for an object"""
        _config = self.schema()(_config)
        return setattr(self, "_config", _config)


class RegistryLoader(object):
    """Manages loading of components for a given registry"""

    def __init__(self, ledfx, cls, package):
        self._package = package
        self._cls = cls
        self._objects = {}
        self._object_id = 1

        self._ledfx = ledfx
        self.import_registry(package)

        # If running in developer mode autoreload the registry when any file
        # within the package changes.
        # Check ledfx is not running as a single exe built using pyinstaller
        # (sys frozen flag).
        if ledfx.dev_enabled() and not currently_frozen():
            import_or_install("watchdog")
            watchdog_events = import_or_install("watchdog.events")
            watchdog_observers = import_or_install("watchdog.observers")

            class RegistryReloadHandler(
                watchdog_events.FileSystemEventHandler
            ):
                def __init__(self, registry):
                    self.registry = registry

                def on_modified(self, event):
                    (_, extension) = os.path.splitext(event.src_path)
                    if extension == ".py":
                        self.registry.reload()

            self.auto_reload_handler = RegistryReloadHandler(self)

            self.observer = watchdog_observers.Observer()
            self.observer.schedule(
                self.auto_reload_handler,
                os.path.dirname(sys.modules[package].__file__),
                recursive=True,
            )
            self.observer.start()

    def import_registry(self, package):
        """
        Imports all the modules in the package thus hydrating
        the registry for the class
        """

        found = self.discover_modules(package)
        _LOGGER.info("Importing {} from {}".format(found, package))
        for name in found:
            importlib.import_module(name)

    def discover_modules(self, package):
        """Discovers all modules in the package"""
        module = importlib.import_module(package)

        found = []
        for _, name, _ in pkgutil.iter_modules(module.__path__, package + "."):
            found.append(name)

        return found

    def __iter__(self):
        return iter(self._objects)

    def types(self):
        """Returns all the type strings in the registry"""
        return list(self._cls.registry().keys())

    def classes(self):
        """Returns all the classes in the registry"""
        return self._cls.registry()

    def get_class(self, type):
        return self._cls.registry()[type]

    def values(self):
        """Returns all the created objects"""
        return self._objects.values()

    def reload_module(self, name):
        if name in sys.modules.keys():
            path = sys.modules[name].__file__
            if path.endswith(".pyc") or path.endswith(".pyo"):
                path = path[:-1]

            try:
                module = importlib.import_module(name, path)
                sys.modules[name] = module
            except SyntaxError as e:
                _LOGGER.error("Failed to reload {}: {}".format(name, e))
        else:
            pass

    def reload(self, force=False):
        """Reloads the registry"""
        found = self.discover_modules(self._package)
        _LOGGER.info("Reloading {} from {}".format(found, self._package))
        for name in found:
            self.reload_module(name)

    def create(self, type, id=None, *args, **kwargs):
        """Loads and creates a object from the registry by type"""

        if type not in self._cls.registry():
            raise AttributeError(
                ("Couldn't find '{}' in the {} registry").format(
                    type, self._cls.__name__.lower()
                )
            )

        id = id or type

        # Find the first valid id based on what is already in the registry
        dupe_id = id
        dupe_index = 1
        while id in self._objects:
            id = "{}-{}".format(dupe_id, dupe_index)
            dupe_index = dupe_index + 1

        # Create the new object based on the registry entires and
        # validate the schema.
        _cls = self._cls.registry().get(type)
        _config = kwargs.pop("config", None)
        if _config is not None:
            _config = _cls.schema()(_config)
            obj = _cls(config=_config, *args, **kwargs)
        else:
            obj = _cls(*args, **kwargs)

        # Attach some common properties
        setattr(obj, "_id", id)
        setattr(obj, "_type", type)

        # Store the object into the internal list and return it
        self._objects[id] = obj
        return obj

    def destroy(self, id):

        if id not in self._objects:
            raise AttributeError(
                ("Object with id '{}' does not exist.").format(id)
            )
        del self._objects[id]

    def get(self, id):
        return self._objects.get(id)
