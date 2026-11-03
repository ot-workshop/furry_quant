"""Process-local startup settings, before scientific dependencies are imported."""

import os
import platform
import sys


def _unavailable_wmi(*args, **kwargs):
    # Python 3.12's platform module catches OSError and uses its built-in
    # Windows API / environment fallback. WMI can block indefinitely here,
    # including during NumPy.testing's import through SciPy and Qlib.
    raise OSError('Use native Windows platform metadata without WMI')


def configure_runtime():
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ.setdefault(name, '1')
    if sys.platform == 'win32' and sys.version_info[:2] == (3, 12) and hasattr(platform, '_wmi_query'):
        # Only this process is changed; never edit Python, Windows or WMI.
        platform._wmi_query = _unavailable_wmi
