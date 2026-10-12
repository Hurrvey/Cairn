"""Trusted Windows gate: join a kill-on-close job before starting any worker code."""

import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes


def main() -> int:
    if os.name != "nt":
        return 126
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", IO),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    request = json.loads(sys.argv[1])
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return 126
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; handle is not inherited.
    if request.get("memory"):
        info.basic.flags |= 0x200
        info.job_memory = request["memory"]
    if not kernel.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info)
    ) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        kernel.CloseHandle(job)
        return 126
    # Only the trusted standard-library gate ran before this assignment. Worker
    # and all descendants inherit the job; no post-start attach race exists.
    child = subprocess.Popen(  # noqa: S603 -- operator-owned argv, only after job assignment
        request["command"],
        stdin=sys.stdin.buffer,
        stdout=sys.stdout.buffer,
        stderr=sys.stderr.buffer,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    result = child.wait()
    # Close kills remaining descendants even when the worker leader exited first.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(result)


if __name__ == "__main__":
    raise SystemExit(main())
