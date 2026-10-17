"""Windows GPU discovery regression tests."""
from unittest.mock import patch
from agent_proxy import host_metrics


def test_nvidia_smi_from_path_wins():
    with patch.object(host_metrics.shutil, "which", return_value=r"C:\\bin\\nvidia-smi.exe"), patch.object(host_metrics.platform, "system", return_value="Windows"):
        assert host_metrics._find_nvidia_smi() == r"C:\\bin\\nvidia-smi.exe"


def test_nvidia_smi_windows_fallback():
    expected = r"C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe"
    with patch.object(host_metrics.shutil, "which", return_value=None), patch.object(host_metrics.platform, "system", return_value="Windows"), patch.dict(host_metrics.os.environ, {"ProgramFiles": r"C:\\Program Files"}), patch.object(host_metrics.os.path, "isfile", side_effect=lambda value: value == expected):
        assert host_metrics._find_nvidia_smi() == expected


def test_nvidia_smi_does_not_probe_windows_path_on_linux():
    with patch.object(host_metrics.shutil, "which", return_value=None), patch.object(host_metrics.platform, "system", return_value="Linux"), patch.object(host_metrics.os.path, "isfile") as isfile:
        assert host_metrics._find_nvidia_smi() is None
        isfile.assert_not_called()
