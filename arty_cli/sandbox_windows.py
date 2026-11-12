import ctypes
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

from arty_cli import tools

PROFILE_NAME = "arty-cli.sandbox"
PROFILE_DESCRIPTION = "Arty CLI command sandbox"
INTERNET_CLIENT = "S-1-15-3-1"
PRIVATE_NETWORK_CLIENT_SERVER = "S-1-15-3-3"
PROFILE_ALREADY_EXISTS = -2147024713
SE_GROUP_ENABLED = 0x4
SECURITY_CAPABILITIES_ATTRIBUTE = 0x00020009
HANDLE_LIST_ATTRIBUTE = 0x00020002
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_SUSPENDED = 0x4
CREATE_UNICODE_ENVIRONMENT = 0x400
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x100
KILL_ON_JOB_CLOSE = 0x2000
EXTENDED_LIMIT_INFORMATION = 9
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
INFINITE = 0xFFFFFFFF
JOB_ENDED_EXIT_CODE = 1
FOLDER_GRANT = "(OI)(CI)M"
WINDOW_OBJECT = 7
DACL_ONLY = 4
GRANT_ACCESS = 1
NO_INHERITANCE = 0
TRUSTEE_IS_SID = 0
TRUSTEE_IS_WELL_KNOWN_GROUP = 5
OBJECT_NAME = 2
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
WINDOW_STATION_RIGHTS = 0x0001 | 0x0002 | 0x0020 | READ_CONTROL
DESKTOP_RIGHTS = 0x0001 | 0x0002 | 0x0040 | READ_CONTROL
CONTAINER_FOLDERS = ("TEMP", "TMP", "LOCALAPPDATA", "APPDATA")
PROJECT_DRIVE = "Project"


class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class SecurityCapabilities(ctypes.Structure):
    _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.POINTER(SidAndAttributes)),
                ("CapabilityCount", wintypes.DWORD), ("Reserved", wintypes.DWORD)]


class StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class ProcessInformation(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount",
                                                         "OtherOperationCount", "ReadTransferCount",
                                                         "WriteTransferCount", "OtherTransferCount")]


class BasicLimitInformation(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BasicLimitInformation), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class Trustee(ctypes.Structure):
    _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", ctypes.c_void_p)]


class ExplicitAccess(ctypes.Structure):
    _fields_ = [("grfAccessPermissions", wintypes.DWORD), ("grfAccessMode", ctypes.c_int),
                ("grfInheritance", wintypes.DWORD), ("Trustee", Trustee)]


class SandboxFailure(OSError):
    pass


def declared(library, name: str, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = list(arguments)
    return function


class WindowsApi:
    def __init__(self):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        handle, dword, boolean, pointer = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
        size_pointer = ctypes.POINTER(ctypes.c_size_t)
        self.create_profile = declared(userenv, "CreateAppContainerProfile", ctypes.c_long, wintypes.LPCWSTR,
                                       wintypes.LPCWSTR, wintypes.LPCWSTR, pointer, dword,
                                       ctypes.POINTER(pointer))
        self.derive_sid = declared(userenv, "DeriveAppContainerSidFromAppContainerName", ctypes.c_long,
                                   wintypes.LPCWSTR, ctypes.POINTER(pointer))
        self.container_folder = declared(userenv, "GetAppContainerFolderPath", ctypes.c_long, wintypes.LPCWSTR,
                                         ctypes.POINTER(wintypes.LPWSTR))
        self.free_task_memory = declared(ole32, "CoTaskMemFree", None, pointer)
        self.sid_to_text = declared(advapi32, "ConvertSidToStringSidW", boolean, pointer,
                                    ctypes.POINTER(wintypes.LPWSTR))
        self.text_to_sid = declared(advapi32, "ConvertStringSidToSidW", boolean, wintypes.LPCWSTR,
                                    ctypes.POINTER(pointer))
        self.local_free = declared(kernel32, "LocalFree", pointer, pointer)
        self.init_attributes = declared(kernel32, "InitializeProcThreadAttributeList", boolean, pointer, dword,
                                        dword, size_pointer)
        self.update_attribute = declared(kernel32, "UpdateProcThreadAttribute", boolean, pointer, dword,
                                         ctypes.c_size_t, pointer, ctypes.c_size_t, pointer, size_pointer)
        self.delete_attributes = declared(kernel32, "DeleteProcThreadAttributeList", None, pointer)
        self.create_process = declared(kernel32, "CreateProcessW", boolean, wintypes.LPCWSTR, wintypes.LPWSTR,
                                       pointer, pointer, boolean, dword, pointer, wintypes.LPCWSTR,
                                       ctypes.POINTER(StartupInfoEx), ctypes.POINTER(ProcessInformation))
        self.create_job = declared(kernel32, "CreateJobObjectW", handle, pointer, wintypes.LPCWSTR)
        self.set_job_limits = declared(kernel32, "SetInformationJobObject", boolean, handle, ctypes.c_int, pointer,
                                       dword)
        self.assign_to_job = declared(kernel32, "AssignProcessToJobObject", boolean, handle, handle)
        self.end_job = declared(kernel32, "TerminateJobObject", boolean, handle, wintypes.UINT)
        self.resume_thread = declared(kernel32, "ResumeThread", dword, handle)
        self.wait_for = declared(kernel32, "WaitForSingleObject", dword, handle, dword)
        self.exit_code_of = declared(kernel32, "GetExitCodeProcess", boolean, handle, ctypes.POINTER(dword))
        self.close_handle = declared(kernel32, "CloseHandle", boolean, handle)
        self.current_thread_id = declared(kernel32, "GetCurrentThreadId", dword)
        self.process_window_station = declared(user32, "GetProcessWindowStation", handle)
        self.thread_desktop = declared(user32, "GetThreadDesktop", handle, dword)
        self.object_information = declared(user32, "GetUserObjectInformationW", boolean, handle, ctypes.c_int,
                                           pointer, dword, ctypes.POINTER(dword))
        self.open_window_station = declared(user32, "OpenWindowStationW", handle, wintypes.LPCWSTR, boolean, dword)
        self.open_desktop = declared(user32, "OpenDesktopW", handle, wintypes.LPCWSTR, dword, boolean, dword)
        self.close_window_station = declared(user32, "CloseWindowStation", boolean, handle)
        self.close_desktop = declared(user32, "CloseDesktop", boolean, handle)
        self.security_of = declared(advapi32, "GetSecurityInfo", dword, handle, ctypes.c_int, dword, pointer,
                                    pointer, ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer))
        self.with_entries = declared(advapi32, "SetEntriesInAclW", dword, wintypes.ULONG,
                                     ctypes.POINTER(ExplicitAccess), pointer, ctypes.POINTER(pointer))
        self.set_security = declared(advapi32, "SetSecurityInfo", dword, handle, ctypes.c_int, dword, pointer,
                                     pointer, pointer, pointer)

    def checked(self, succeeded, action: str) -> None:
        if not succeeded:
            raise SandboxFailure(f"{action} failed: {ctypes.WinError(ctypes.get_last_error())}")

    def container_sid(self) -> ctypes.c_void_p:
        sid = ctypes.c_void_p()
        result = self.create_profile(PROFILE_NAME, PROFILE_NAME, PROFILE_DESCRIPTION, None, 0, ctypes.byref(sid))
        if result == PROFILE_ALREADY_EXISTS:
            result = self.derive_sid(PROFILE_NAME, ctypes.byref(sid))
        if result != 0:
            raise SandboxFailure(f"couldn't set up the sandbox profile (HRESULT {result & 0xFFFFFFFF:#010x})")
        return sid

    def sid_text(self, sid) -> str:
        text = wintypes.LPWSTR()
        self.checked(self.sid_to_text(sid, ctypes.byref(text)), "ConvertSidToStringSidW")
        try:
            return text.value
        finally:
            self.local_free(ctypes.cast(text, ctypes.c_void_p))

    def sid_from_text(self, text: str) -> ctypes.c_void_p:
        sid = ctypes.c_void_p()
        self.checked(self.text_to_sid(text, ctypes.byref(sid)), "ConvertStringSidToSidW")
        return sid

    def folder_of(self, sid_text: str) -> str:
        folder = wintypes.LPWSTR()
        result = self.container_folder(sid_text, ctypes.byref(folder))
        if result != 0:
            raise SandboxFailure(f"couldn't find the sandbox's own folder (HRESULT {result & 0xFFFFFFFF:#010x})")
        try:
            return folder.value
        finally:
            self.free_task_memory(ctypes.cast(folder, ctypes.c_void_p))


    def object_name(self, handle) -> str:
        name = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        self.checked(self.object_information(handle, OBJECT_NAME, name, ctypes.sizeof(name), ctypes.byref(needed)),
                     "GetUserObjectInformationW")
        return name.value

    def allow_on_window_object(self, handle, sid, rights: int) -> None:
        current, descriptor, combined = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        failure = self.security_of(handle, WINDOW_OBJECT, DACL_ONLY, None, None, ctypes.byref(current), None,
                                   ctypes.byref(descriptor))
        if failure:
            raise SandboxFailure(f"GetSecurityInfo failed: {ctypes.WinError(failure)}")
        try:
            entry = ExplicitAccess(rights, GRANT_ACCESS, NO_INHERITANCE,
                                   Trustee(None, 0, TRUSTEE_IS_SID, TRUSTEE_IS_WELL_KNOWN_GROUP, sid))
            failure = self.with_entries(1, ctypes.byref(entry), current, ctypes.byref(combined))
            if failure:
                raise SandboxFailure(f"SetEntriesInAclW failed: {ctypes.WinError(failure)}")
            failure = self.set_security(handle, WINDOW_OBJECT, DACL_ONLY, None, None, combined, None)
            if failure:
                raise SandboxFailure(f"SetSecurityInfo failed: {ctypes.WinError(failure)}")
        finally:
            self.local_free(descriptor)
            self.local_free(combined)

    def allow_on_session_desktop(self, sid) -> None:
        station_name = self.object_name(self.process_window_station())
        desktop_name = self.object_name(self.thread_desktop(self.current_thread_id()))
        station = self.open_window_station(station_name, False, READ_CONTROL | WRITE_DAC)
        self.checked(station, f"OpenWindowStationW({station_name})")
        try:
            self.allow_on_window_object(station, sid, WINDOW_STATION_RIGHTS)
        finally:
            self.close_window_station(station)
        desktop = self.open_desktop(desktop_name, 0, False, READ_CONTROL | WRITE_DAC)
        self.checked(desktop, f"OpenDesktopW({desktop_name})")
        try:
            self.allow_on_window_object(desktop, sid, DESKTOP_RIGHTS)
        finally:
            self.close_desktop(desktop)


def problem() -> str:
    if sys.platform != "win32":
        return "the Windows sandbox only runs on Windows"
    try:
        api = WindowsApi()
        api.sid_text(api.container_sid())
    except (OSError, AttributeError) as error:
        return f"Windows AppContainer isn't available: {error}"
    return ""


def too_broad(root: Path, home: Path) -> str:
    if Path(home).is_relative_to(Path(root)):
        return f"{root} holds your whole user folder, and the sandbox would be able to change all of it"
    return ""


def prepared(root: Path, home: Path) -> str:
    broad = too_broad(root, home)
    if broad:
        return broad
    try:
        api = WindowsApi()
        sid = api.container_sid()
        sid_text = api.sid_text(sid)
        api.allow_on_session_desktop(sid)
    except OSError as error:
        return str(error)
    finished = subprocess.run([tools.windows_system_program("icacls.exe"), str(root), "/grant",
                               f"*{sid_text}:{FOLDER_GRANT}", "/Q"], capture_output=True, text=True, errors="replace")
    if finished.returncode != 0:
        return f"couldn't let the sandbox write to {root}: {(finished.stdout + finished.stderr).strip()[:200]}"
    return ""


def in_project_drive(root: Path, command: str) -> str:
    quoted_root = str(root).replace("'", "''")
    return (f"New-PSDrive -Name {PROJECT_DRIVE} -PSProvider FileSystem -Root '{quoted_root}' | Out-Null; "
            f"Set-Location {PROJECT_DRIVE}:\\; {command}")


def command_line(root: Path, command: str) -> str:
    return subprocess.list2cmdline(tools.powershell_arguments(in_project_drive(root, command)))


def environment_block(environment: dict, container_folder: str) -> str:
    table = dict(environment)
    for name in CONTAINER_FOLDERS:
        table[name] = container_folder
    lines = [f"{name}={value}" for name, value in sorted(table.items(), key=lambda item: item[0].upper())]
    return "\0".join(lines) + "\0\0"


def capability_sids(api: WindowsApi, network: bool) -> list:
    return [api.sid_from_text(text) for text in (INTERNET_CLIENT, PRIVATE_NETWORK_CLIENT_SERVER)] if network else []


def attribute_list(api: WindowsApi, entries: list) -> ctypes.Array:
    size = ctypes.c_size_t()
    api.init_attributes(None, len(entries), 0, ctypes.byref(size))
    buffer = (ctypes.c_byte * size.value)()
    api.checked(api.init_attributes(buffer, len(entries), 0, ctypes.byref(size)), "InitializeProcThreadAttributeList")
    for attribute, value in entries:
        api.checked(api.update_attribute(buffer, 0, attribute, ctypes.byref(value), ctypes.sizeof(value), None, None),
                    "UpdateProcThreadAttribute")
    return buffer


def killing_job(api: WindowsApi) -> int:
    job = api.create_job(None, None)
    api.checked(job, "CreateJobObjectW")
    limits = ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE
    api.checked(api.set_job_limits(job, EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)),
                "SetInformationJobObject")
    return job


def inheritable_handle(descriptor: int) -> int:
    import msvcrt
    handle = msvcrt.get_osfhandle(descriptor)
    os.set_handle_inheritable(handle, True)
    return handle


def start(root: Path, command: str, network: bool) -> "SandboxedProcess":
    api = WindowsApi()
    sid = api.container_sid()
    container_folder = api.folder_of(api.sid_text(sid))
    capabilities = capability_sids(api, network)
    granted_capabilities = (SidAndAttributes * max(len(capabilities), 1))(
        *[SidAndAttributes(capability, SE_GROUP_ENABLED) for capability in capabilities])
    security = SecurityCapabilities(sid, granted_capabilities if capabilities else None, len(capabilities), 0)
    read_end, write_end = os.pipe()
    nothing = os.open(os.devnull, os.O_RDONLY)
    try:
        handles = (wintypes.HANDLE * 2)(inheritable_handle(write_end), inheritable_handle(nothing))
        attributes = attribute_list(api, [(SECURITY_CAPABILITIES_ATTRIBUTE, security), (HANDLE_LIST_ATTRIBUTE, handles)])
        startup = StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(StartupInfoEx)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = handles[1]
        startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = handles[0]
        startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        environment = ctypes.create_unicode_buffer(environment_block(os.environ, container_folder))
        flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
        information = ProcessInformation()
        line = ctypes.create_unicode_buffer(command_line(root, command))
        try:
            api.checked(api.create_process(None, line, None, None, True, flags, environment, str(root),
                                           ctypes.byref(startup), ctypes.byref(information)), "CreateProcessW")
        finally:
            api.delete_attributes(attributes)
        job = killing_job(api)
        api.checked(api.assign_to_job(job, information.hProcess), "AssignProcessToJobObject")
        api.resume_thread(information.hThread)
        api.close_handle(information.hThread)
    except BaseException:
        os.close(read_end)
        raise
    finally:
        os.close(write_end)
        os.close(nothing)
    output = open(read_end, encoding="utf-8", errors="replace")
    return SandboxedProcess(api, information.hProcess, job, information.dwProcessId, output, command)


class SandboxedProcess:
    def __init__(self, api: WindowsApi, process_handle, job_handle, pid: int, output, command: str):
        self.api = api
        self.process_handle = process_handle
        self.job_handle = job_handle
        self.pid = pid
        self.stdout = output
        self.args = command
        self.returncode = None
        self.collected = []
        self.reader = None
        self.lock = threading.RLock()

    def poll(self):
        with self.lock:
            if self.returncode is None and self.api.wait_for(self.process_handle, 0) == WAIT_OBJECT_0:
                self.finish()
            return self.returncode

    def finish(self) -> None:
        code = wintypes.DWORD()
        self.api.exit_code_of(self.process_handle, ctypes.byref(code))
        self.returncode = code.value
        self.end_tree()
        self.api.close_handle(self.process_handle)

    def wait(self, timeout=None):
        milliseconds = INFINITE if timeout is None else int(timeout * 1000)
        if self.returncode is None and self.api.wait_for(self.process_handle, milliseconds) == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.poll()

    def read_all(self) -> None:
        self.collected.append(self.stdout.read())

    def communicate(self, timeout=None):
        if self.reader is None:
            self.reader = threading.Thread(target=self.read_all, daemon=True)
            self.reader.start()
        self.wait(timeout)
        self.reader.join()
        return "".join(self.collected), None

    def end_tree(self) -> None:
        with self.lock:
            if self.job_handle:
                self.api.end_job(self.job_handle, JOB_ENDED_EXIT_CODE)
                self.api.close_handle(self.job_handle)
                self.job_handle = None
