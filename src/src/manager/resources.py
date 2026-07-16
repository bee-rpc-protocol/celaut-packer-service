# I/O Big Data utils.
import gc
import os
import re
import time
from time import sleep
from threading import Lock, RLock
import threading
from typing import Optional
import psutil
from protos import celaut_pb2

class Singleton(type):
    _instances = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                # another thread could have created the instance
                # before we acquired the lock. So check that the
                # instance is still nonexistent.
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


mem_manager = lambda len, timeout=None: IOBigData().lock(len=len, timeout=timeout)

PREVENT_KILL_WAIT_TIME = 5 # seconds

# service.json reserves only 0.5 GB of RAM up front (at_init == at_most). When a
# pack needs more than the currently-locked amount, the packer asks its own nodo
# — the authority — to hotplug extra RAM into this microVM instead of failing
# against the (older, static) system-available accounting.
#
# The grow call is delegated to the canonical node_controller library
# (celaut-project/libraries), exactly like celaut-basics/demo-service does in its
# /modify_max_memory endpoint: build a Controller from the config file the node
# mounts at /__config__ (it carries the initial resources + gateway URL) and call
# Controller.modify_resources({'max': needed, 'min': 0}). We no longer hand-roll
# the Gateway ModifyServiceSystemResources RPC.
NODE_CONFIG_FILE = os.environ.get("PACKER_CONFIG_FILE", "/__config__")
# App dir handed to the Controller (where it keeps its __services__/__metadata__
# scratch dirs). Mirrors demo-service's DIR="service".
NODE_APP_DIR = os.environ.get("PACKER_APP_DIR", "service")
# Extra RAM requested above the strict need, so a grow doesn't land exactly on
# the edge and immediately need another round-trip.
GROW_HEADROOM_BYTES = int(os.environ.get("PACKER_GROW_HEADROOM_BYTES", 128 * 1024 * 1024))


class NodeControllerResourceManager:
    """Grow this microVM's RAM through celaut's ``node_controller`` library.

    Mirrors celaut-basics/demo-service: it lazily builds a
    ``node_controller.controller.Controller`` from the config file the node mounts
    at ``/__config__`` (initial resources + gateway URL) and calls
    ``Controller.modify_resources({'max': needed, 'min': 0})`` to hotplug memory.
    The node hotplugs the granted RAM, which then shows up in the system-available
    accounting used by ``IOBigData.ram_pool``.

    It is a no-op (returns ``None``) whenever there is no node to reach — no config
    file, or ``node_controller``/its deps aren't importable (dev boxes, unit
    tests) — so the original static locker behaviour is preserved verbatim.
    """

    def __init__(self, config_file: str = NODE_CONFIG_FILE, app_dir: str = NODE_APP_DIR,
                 log=lambda message: print(message)) -> None:
        self._config_file = config_file
        self._app_dir = app_dir
        self._log = log
        self._controller = None
        self._resolved = False

    def _get_controller(self):
        if self._resolved:
            return self._controller
        self._resolved = True
        try:
            if not self._config_file or not os.path.exists(self._config_file):
                return None  # not running inside a node -> keep static behaviour
            from node_controller.controller.controller import Controller
            # Only the resource-modify path is needed, so skip the dependency- and
            # resource-manager background machinery the demo service spins up.
            self._controller = Controller(
                debug=lambda m: self._log(f"[node_controller] {m}"),
                app_dir=self._app_dir,
                config_file=self._config_file,
                default_dependency_manager=False,
                default_resource_manager=False,
            )
        except Exception as e:
            self._log(f"[MEM] node_controller unavailable for modify_resources: {e}")
            self._controller = None
        return self._controller

    def modify_resources(self, min_bytes: int, max_bytes: Optional[int] = None) -> Optional[int]:
        """Ask the node to hotplug memory up to ``max_bytes`` (mirrors demo
        service: max=needed, min=0). Returns the granted ``mem_limit`` in bytes,
        or ``None`` when no request could be made."""
        controller = self._get_controller()
        if controller is None:
            return None
        target = int(max_bytes if max_bytes is not None else min_bytes)
        try:
            from google.protobuf.json_format import MessageToDict
            _resources, _gas_amount = controller.modify_resources(
                resources={'max': target, 'min': 0}
            )
            granted = int(MessageToDict(_resources).get("memLimit", 0))
            self._log(
                f"[MEM] node granted mem_limit={IOBigData.convert_size(granted)} "
                f"(requested max={IOBigData.convert_size(target)})"
            )
            return granted
        except Exception as e:
            self._log(f"[MEM] modify_resources call failed: {e}")
            return None

class IOBigData(metaclass=Singleton):
    class RamLocker(object):
        def __init__(self, len, iobd, timeout=None):
            self.len = len
            self.iobd = iobd
            self.timeout = timeout

        def __enter__(self):
            self.iobd.lock_ram(ram_amount=self.len, timeout=self.timeout)
            return self

        def unlock(self, amount: int):
            self.iobd.unlock_ram(ram_amount=amount)
            self.len -= amount

        def __exit__(self, type, value, traceback):
            self.iobd.unlock_ram(ram_amount=self.len)
            gc.collect()

    def __init__(self,
                 log=lambda message: print(message),
                 ram_pool_method=None,
                 resource_manager=None
                 ) -> None:

        self._initial_python_rss_bytes = _python_rss_bytes()  # Consumo del intérprete de python al iniciar el proceso, tomado como referencia de uso base para evitar el doble conteo con ram_locked. Se considera que esto es lo que gasta fuera del locked.

        def default_ram_pool():
            sys_available = psutil.virtual_memory().available
            nodo_rss, nodo_reserved = _get_nodo_ch_memory_stats()
            
            # Solo restamos el crecimiento potencial de las VMs
            potential_vm_growth = max(0, nodo_reserved - nodo_rss) 
            
            # Compensamos el consumo actual del daemon para evitar el doble conteo con ram_locked
            daemon_growth = min(self.ram_locked, _python_rss_bytes() - self._initial_python_rss_bytes)  #  El min se usa para evitar que se hubiera restado ram_locked pero aún no se reflejara en el RSS del proceso, lo que podría llevar a un conteo negativo (mas disponible del que realmente hay).
            
            return sys_available - potential_vm_growth + daemon_growth

        self.ram_pool = ram_pool_method if ram_pool_method is not None else default_ram_pool

        self.log = log
        self.ram_locked = 0
        self.get_ram_avaliable = lambda: self.ram_pool() - self.ram_locked
        self.amount_lock = RLock()

        self.waiting_bytes = 0
        self.wait_lock = Lock()

        # Node-authority growth: when the static pool can't cover a lock, ask the
        # node to hotplug more RAM via node_controller. Defaults to the real
        # controller-backed manager, which no-ops when no node/config is reachable
        # (dev boxes, tests).
        self.resource_manager = resource_manager if resource_manager is not None else NodeControllerResourceManager(log=log)
        self._last_growth_target = 0  # largest mem_limit already requested from the node

    # General methods.

    def set_log(self, log=lambda message: print(message)) -> None:
        self.log = log
        if isinstance(getattr(self, "resource_manager", None), NodeControllerResourceManager):
            self.resource_manager._log = log

    def set_resource_manager(self, resource_manager) -> None:
        self.resource_manager = resource_manager

    def _request_growth(self, ram_amount: int) -> None:
        """When the static pool can't satisfy ``ram_amount``, ask the node (the
        authority) to hotplug more RAM into this microVM. No-op when no manager
        or gateway is available, which leaves the original static wait/fail
        behaviour untouched."""
        rm = self.resource_manager
        if rm is None:
            return
        with self.amount_lock:
            # Total the daemon must hold at once: everything already locked, plus
            # this request, plus a little headroom.
            needed_total = self.ram_locked + ram_amount + GROW_HEADROOM_BYTES
        if needed_total <= self._last_growth_target:
            return  # already asked the node for at least this much this run
        self._last_growth_target = needed_total
        self.log(f"[MEM] static pool short for {IOBigData.convert_size(ram_amount)}; "
                 f"requesting node grow to {IOBigData.convert_size(needed_total)}")
        granted = rm.modify_resources(needed_total, needed_total)
        if granted is not None:
            self.log_snapshot(context=f"after-modify-resources granted={granted}")

    @staticmethod
    def convert_size(size_bytes):
        import math
        if size_bytes == 0:
            return "0B"
        size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
        try:
            i = int(math.floor(math.log(size_bytes, 1024)))
            p = math.pow(1024, i)
            s = round(size_bytes / p, 2)
            return "%s %s" % (s, size_name[i])
        except ValueError:
            return "%s %s" % (size_bytes, size_name[0])

    def snapshot(self) -> dict:
        system_available = int(psutil.virtual_memory().available)
        with self.amount_lock:
            ram_locked = int(self.ram_locked)
            pool_available = int(self.ram_pool())
            effective_available = int(self.get_ram_avaliable())
            with self.wait_lock:
                waiting = int(self.waiting_bytes)
                
        return {
            "pid": os.getpid(),
            "system_available": system_available,
            "pool_available": pool_available,
            "ram_locked": ram_locked,
            "effective_available": effective_available,
            "waiting": waiting,
        }

    def log_snapshot(self, context: str) -> None:
        snapshot = self.snapshot()
        self.log(
            "[MEM] "
            f"{context} | pid={snapshot['pid']} | "
            f"system_available={IOBigData.convert_size(snapshot['system_available'])} | "
            f"pool_available={IOBigData.convert_size(snapshot['pool_available'])} | "
            f"ram_locked={IOBigData.convert_size(snapshot['ram_locked'])} | "
            f"effective_available={IOBigData.convert_size(snapshot['effective_available'])} | "
            f"waiting={IOBigData.convert_size(snapshot['waiting'])}"
        )

    def __stats(self, message: str, comments: bool = True):
        if comments:
            with self.amount_lock:
                nodo_rss, nodo_reserved = _get_nodo_ch_memory_stats()
                current_python_rss = _python_rss_bytes()
                
                self.log('\n--------- ' + message + ' -------------')
                self.log('SYSTEM AVAILABLE -> ' + IOBigData.convert_size(psutil.virtual_memory().available))
                self.log('VMACHINES RSS     -> ' + IOBigData.convert_size(nodo_rss))
                self.log('VMACHINES RESERVED -> ' + IOBigData.convert_size(nodo_reserved))
                self.log('VMACHINES NOT USED   -> ' + IOBigData.convert_size(max(0, nodo_reserved - nodo_rss)))
                self.log('DAEMON RSS      -> ' + IOBigData.convert_size(current_python_rss))
                self.log('DAEMON RSS INI  -> ' + IOBigData.convert_size(self._initial_python_rss_bytes))
                self.log('DAEMON RSS ON LOCK -> ' + IOBigData.convert_size(max(0, current_python_rss - self._initial_python_rss_bytes)))
                self.log('RAM POOL        -> ' + IOBigData.convert_size(self.ram_pool()))
                self.log('RAM LOCKED      -> ' + IOBigData.convert_size(self.ram_locked))
                self.log('RAM AVAILABLE   -> ' + IOBigData.convert_size(self.get_ram_avaliable()))
                with self.wait_lock:
                    self.log('RAM WAITING     -> ' + IOBigData.convert_size(self.waiting_bytes))
                self.log('-----------------------------------------\n')

    # Gas manager methods.
    def __push_wait_list(self, l: int):
        with self.wait_lock:
            self.waiting_bytes += l

    def __pop_wait_list(self, l: int):
        with self.wait_lock:
            self.waiting_bytes -= l
            if self.waiting_bytes < 0:
                self.waiting_bytes = 0

    def __can_lock_ram(self, ram_amount: int, *, inclusive: bool) -> bool:
        with self.amount_lock:
            available = self.get_ram_avaliable()
            return available >= ram_amount if inclusive else available > ram_amount

    # Manage resources methods.

    def lock(self, len, timeout=None):
        return self.RamLocker(len=len, iobd=self, timeout=timeout)

    # Lock_ram y unlock_Ram son usados en __enter__ y __exit__ de RamLocker.
    def lock_ram(self, ram_amount: int, wait: bool = True, timeout: Optional[float] = None):
        self.__stats('want lock ' + IOBigData.convert_size(ram_amount))
        self.__push_wait_list(l=ram_amount)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                self.__stats('go to lock ' + IOBigData.convert_size(ram_amount))
                # The declared 0.5 GB reservation is only a floor. If the static
                # pool can't cover this lock, ask the node to hotplug more RAM
                # before we block/fail against the older static accounting.
                if not self.__can_lock_ram(ram_amount=ram_amount, inclusive=True):
                    self._request_growth(ram_amount)

                if wait:
                    self.wait_to_prevent_kill(len=ram_amount, deadline=deadline)

                elif not self.__can_lock_ram(ram_amount=ram_amount, inclusive=True):
                    raise Exception

                with self.amount_lock:
                    if self.__can_lock_ram(ram_amount=ram_amount, inclusive=True):
                        self.ram_locked += ram_amount
                    else:
                        continue
                break
        except Exception:
            self.__pop_wait_list(l=ram_amount)
            raise
        self.__pop_wait_list(l=ram_amount)
        self.__stats('locked ' + IOBigData.convert_size(ram_amount))

    def unlock_ram(self, ram_amount: int):
        with self.amount_lock:
            if ram_amount < self.ram_locked:
                self.ram_locked -= ram_amount
            else:
                self.ram_locked = 0

        self.__stats('unlocked ' + IOBigData.convert_size(ram_amount))

    def prevent_kill(self, len: int) -> bool:
        b = self.__can_lock_ram(ram_amount=len, inclusive=False)
        self.__stats('[prevent kill] Try to take ' + IOBigData.convert_size(len) + '. Takes it:' + str(b))
        return b

    def wait_to_prevent_kill(self, len: int, deadline: Optional[float] = None) -> None:
        while True:
            if not self.__can_lock_ram(ram_amount=len, inclusive=True):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting to unlock memory for {IOBigData.convert_size(len)}"
                    )
                sleep(PREVENT_KILL_WAIT_TIME)
            else:
                return

def could_ve_this_sysreq(sysreq: celaut_pb2.Sysresources) -> bool:
    return IOBigData().prevent_kill(len=sysreq.mem_limit)  

def _get_nodo_ch_memory_stats() -> tuple[int, int]:
    """Devuelve una tupla con (total_rss_bytes, total_reserved_bytes) en una sola pasada."""
    total_rss = 0
    total_reserved = 0
    for p in psutil.process_iter(["name", "cmdline", "memory_info"]):
        try:
            cmdline = " ".join(p.info.get("cmdline") or [])
            if "nodo-ch" in cmdline:
                mem_info = p.info.get("memory_info")
                if mem_info:
                    total_rss += mem_info.rss
                total_reserved += __parse_memory_from_cmdline(cmdline)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, TypeError):
            continue
    return total_rss, total_reserved

def _python_rss_bytes():
    return psutil.Process(os.getpid()).memory_info().rss

def __parse_memory_from_cmdline(cmdline: str) -> int:
    """Parsea la memoria reservada del cmdline de nodo-ch."""
    match = re.search(r'--memory\s+size=(\d+)([KMGT]?)', cmdline, re.IGNORECASE)
    if not match:
        return 0
    
    value = int(match.group(1))
    unit = match.group(2).upper() if match.group(2) else 'M'  
    
    multipliers = {
        'K': 1024,
        'M': 1024 ** 2,
        'G': 1024 ** 3,
        'T': 1024 ** 4,
    }
    
    return value * multipliers.get(unit, 1024 ** 2)