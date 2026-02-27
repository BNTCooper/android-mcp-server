import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from PIL import Image as PILImage
from ppadb.client import Client as AdbClient


class AdbDeviceManager:
    def __init__(self, device_name: str | None = None, exit_on_error: bool = True) -> None:
        """
        Initialize the ADB Device Manager

        Args:
            device_name: Optional name/serial of the device to manage.
                         If None, attempts to auto-select if only one device is available.
            exit_on_error: Whether to exit the program if device initialization fails
        """
        if not self.check_adb_installed():
            error_msg = "adb is not installed or not in PATH. Please install adb and ensure it is in your PATH."
            if exit_on_error:
                print(error_msg, file=sys.stderr)
                sys.exit(1)
            else:
                raise RuntimeError(error_msg)

        available_devices = self.get_available_devices()
        if not available_devices:
            error_msg = "No devices connected. Please connect a device and try again."
            if exit_on_error:
                print(error_msg, file=sys.stderr)
                sys.exit(1)
            else:
                raise RuntimeError(error_msg)

        selected_device_name: str | None = None

        if device_name:
            if device_name not in available_devices:
                error_msg = f"Device {device_name} not found. Available devices: {available_devices}"
                if exit_on_error:
                    print(error_msg, file=sys.stderr)
                    sys.exit(1)
                else:
                    raise RuntimeError(error_msg)
            selected_device_name = device_name
        else:  # No device_name provided, try auto-selection
            if len(available_devices) == 1:
                selected_device_name = available_devices[0]
                print(
                    f"No device specified, automatically selected: {selected_device_name}")
            elif len(available_devices) > 1:
                error_msg = f"Multiple devices connected: {available_devices}. Please specify a device in config.yaml or connect only one device."
                if exit_on_error:
                    print(error_msg, file=sys.stderr)
                    sys.exit(1)
                else:
                    raise RuntimeError(error_msg)
            # If len(available_devices) == 0, it's already caught by the earlier check

        # At this point, selected_device_name should always be set due to the logic above
        # Initialize the device
        self.device = AdbClient().device(selected_device_name)
        self.device_serial = selected_device_name
        self.flutter_process: subprocess.Popen | None = None
        self.flutter_log_path: str | None = None
        self._flutter_log_handle = None

    @staticmethod
    def check_adb_installed() -> bool:
        """Check if ADB is installed on the system."""
        try:
            subprocess.run(["adb", "version"], check=True,
                           stdout=subprocess.PIPE)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    @staticmethod
    def get_available_devices() -> list[str]:
        """Get a list of available devices."""
        return [device.serial for device in AdbClient().devices()]

    def get_packages(self) -> str:
        command = "pm list packages"
        packages = self.device.shell(command).strip().split("\n")
        result = [package[8:] for package in packages]
        output = "\n".join(result)
        return output

    def get_package_action_intents(self, package_name: str) -> list[str]:
        command = f"dumpsys package {package_name}"
        output = self.device.shell(command)

        resolver_table_start = output.find("Activity Resolver Table:")
        if resolver_table_start == -1:
            return []
        resolver_section = output[resolver_table_start:]

        non_data_start = resolver_section.find("\n  Non-Data Actions:")
        if non_data_start == -1:
            return []

        section_end = resolver_section[non_data_start:].find("\n\n")
        if section_end == -1:
            non_data_section = resolver_section[non_data_start:]
        else:
            non_data_section = resolver_section[
                non_data_start: non_data_start + section_end
            ]

        actions = []
        for line in non_data_section.split("\n"):
            line = line.strip()
            if line.startswith("android.") or line.startswith("com."):
                actions.append(line)

        return actions

    def execute_adb_shell_command(self, command: str) -> str:
        """Executes an ADB command and returns the output."""
        if command.startswith("adb shell "):
            command = command[10:]
        elif command.startswith("adb "):
            command = command[4:]
        result = self.device.shell(command)
        return result

    def launch_app(self, package_name: str, activity_name: str | None = None, stop_first: bool = False) -> str:
        """Launches an Android app by package name and optional activity."""
        output_parts = []
        if stop_first:
            self.device.shell(f"am force-stop {package_name}")
            output_parts.append(f"Force-stopped {package_name}")

        if activity_name:
            component = activity_name if "/" in activity_name else f"{package_name}/{activity_name}"
            launch_output = self.device.shell(f"am start -n {component}")
        else:
            launch_output = self.device.shell(
                f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1"
            )

        launch_output = launch_output.strip()
        if launch_output:
            output_parts.append(launch_output)

        if not output_parts:
            return f"Launch command sent for {package_name}"

        return "\n".join(output_parts)

    @staticmethod
    def _tail_file(file_path: str, lines: int = 60) -> str:
        if not os.path.exists(file_path):
            return f"No log file found at {file_path}"
        with open(file_path, encoding="utf-8", errors="replace") as handle:
            file_lines = handle.readlines()
        if not file_lines:
            return "(log file is empty)"
        return "".join(file_lines[-lines:]).rstrip()

    def _resolve_flutter_executable(self, flutter_executable: str) -> str:
        # If an explicit file path is provided, use it directly.
        if os.path.sep in flutter_executable or (
            os.path.altsep and os.path.altsep in flutter_executable
        ):
            if not os.path.exists(flutter_executable):
                raise RuntimeError(
                    f"Flutter executable not found at {flutter_executable}"
                )
            return flutter_executable

        resolved = shutil.which(flutter_executable)
        if resolved:
            return resolved

        raise RuntimeError(
            f"Could not find '{flutter_executable}' on PATH. "
            "Pass an absolute flutter executable path."
        )

    def _cleanup_flutter_process_state(self) -> None:
        if self._flutter_log_handle and not self._flutter_log_handle.closed:
            self._flutter_log_handle.close()
        self._flutter_log_handle = None
        self.flutter_process = None

    def start_flutter_run(
        self,
        project_dir: str,
        target: str = "lib/main.dart",
        flutter_executable: str = "flutter",
        additional_args: str | None = None,
        startup_wait_seconds: int = 8,
    ) -> str:
        """
        Start `flutter run` in a managed subprocess tied to the selected device.
        """
        if self.flutter_process and self.flutter_process.poll() is None:
            return (
                f"Flutter run already active (pid: {self.flutter_process.pid}). "
                "Use hot_reload_flutter_run / hot_restart_flutter_run or stop_flutter_run."
            )

        if self.flutter_process and self.flutter_process.poll() is not None:
            self._cleanup_flutter_process_state()

        if not os.path.isdir(project_dir):
            raise RuntimeError(f"Project directory not found: {project_dir}")

        flutter_cmd = self._resolve_flutter_executable(flutter_executable)

        command = [flutter_cmd, "run", "-d", self.device_serial, "-t", target]
        if additional_args:
            command.extend(shlex.split(additional_args, posix=os.name != "nt"))

        self.flutter_log_path = os.path.join(project_dir, ".mcp_flutter_run.log")
        self._flutter_log_handle = open(self.flutter_log_path, "w", encoding="utf-8")

        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.flutter_process = subprocess.Popen(
            command,
            cwd=project_dir,
            stdin=subprocess.PIPE,
            stdout=self._flutter_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )

        time.sleep(max(0, startup_wait_seconds))

        if self.flutter_process.poll() is not None:
            exit_code = self.flutter_process.returncode
            failure_tail = self._tail_file(self.flutter_log_path, 80)
            self._cleanup_flutter_process_state()
            return (
                f"flutter run exited early with code {exit_code}.\n"
                f"Log tail:\n{failure_tail}"
            )

        return (
            f"Started flutter run (pid: {self.flutter_process.pid}) on device {self.device_serial}.\n"
            f"Log file: {self.flutter_log_path}"
        )

    def hot_reload_flutter_run(self) -> str:
        """Send hot reload command (`r`) to the managed flutter run process."""
        if not self.flutter_process or self.flutter_process.poll() is not None:
            return "No active flutter run process. Start one with start_flutter_run first."

        if not self.flutter_process.stdin:
            return "Flutter process stdin is unavailable; cannot send hot reload command."

        self.flutter_process.stdin.write("r\n")
        self.flutter_process.stdin.flush()
        return "Hot reload command sent to flutter run."

    def hot_restart_flutter_run(self) -> str:
        """Send hot restart command (`R`) to the managed flutter run process."""
        if not self.flutter_process or self.flutter_process.poll() is not None:
            return "No active flutter run process. Start one with start_flutter_run first."

        if not self.flutter_process.stdin:
            return "Flutter process stdin is unavailable; cannot send hot restart command."

        self.flutter_process.stdin.write("R\n")
        self.flutter_process.stdin.flush()
        return "Hot restart command sent to flutter run."

    def stop_flutter_run(self, graceful_wait_seconds: int = 10) -> str:
        """Stop the managed flutter run process, trying graceful quit first."""
        if not self.flutter_process or self.flutter_process.poll() is not None:
            self._cleanup_flutter_process_state()
            return "No active flutter run process."

        pid = self.flutter_process.pid
        if self.flutter_process.stdin:
            self.flutter_process.stdin.write("q\n")
            self.flutter_process.stdin.flush()

        try:
            self.flutter_process.wait(timeout=max(1, graceful_wait_seconds))
            stopped_gracefully = True
        except subprocess.TimeoutExpired:
            self.flutter_process.kill()
            self.flutter_process.wait(timeout=5)
            stopped_gracefully = False

        self._cleanup_flutter_process_state()
        if stopped_gracefully:
            return f"Stopped flutter run gracefully (pid: {pid})."
        return f"Force-killed flutter run process (pid: {pid})."

    def get_flutter_run_log(self, lines: int = 60) -> str:
        """Read the tail of the managed flutter run log file."""
        if not self.flutter_log_path:
            return "No flutter log available yet. Start flutter run first."

        if self._flutter_log_handle and not self._flutter_log_handle.closed:
            self._flutter_log_handle.flush()

        return self._tail_file(self.flutter_log_path, lines=max(1, lines))

    def _read_pid_logcat(self, package_name: str) -> str | None:
        """Read logcat lines scoped to a package PID."""
        pid_output = self.device.shell(f"pidof {package_name}").strip()
        if not pid_output:
            return None

        pid = pid_output.split()[0]
        result = subprocess.run(
            ["adb", "-s", self.device_serial, "logcat", "-d", "--pid", pid],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None

        return result.stdout

    def _discover_vm_service_debug_url(self, package_name: str) -> str | None:
        """
        Discover full Dart VM service URL including auth token path from logcat.
        """
        log_output = self._read_pid_logcat(package_name)
        if not log_output:
            return None

        matches = re.findall(
            r"The Dart VM [Ss]ervice is listening on (http://127\.0\.0\.1:\d+/[A-Za-z0-9_\-]+=*/)",
            log_output,
        )
        if not matches:
            return None

        return matches[-1]

    @staticmethod
    def _extract_vm_service_urls(text: str) -> list[str]:
        return re.findall(
            r"--vm-service-uri=(http://127\.0\.0\.1:\d+/[A-Za-z0-9_\-]+=*/)",
            text,
        )

    def _discover_vm_service_debug_url_from_host(self) -> str | None:
        """
        Discover VM service URL from host-side flutter/dart processes.
        This is typically the most reliable source when VS Code launches flutter.
        """
        try:
            if os.name == "nt":
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        "Get-CimInstance Win32_Process | "
                        "Where-Object { $_.CommandLine -like '*--vm-service-uri=http://127.0.0.1:*' } | "
                        "Sort-Object CreationDate | "
                        "Select-Object -ExpandProperty CommandLine",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            else:
                result = subprocess.run(
                    ["ps", "-eo", "args"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
        except Exception:
            return None

        if result.returncode != 0 or not result.stdout:
            return None

        urls = self._extract_vm_service_urls(result.stdout)
        if not urls:
            return None
        return urls[-1]

    def _discover_vm_service_port(self, package_name: str) -> int | None:
        """
        Discover Dart VM service port for a running package by parsing logcat.
        Returns the most recent matching port if found.
        """
        log_output = self._read_pid_logcat(package_name)
        if not log_output:
            return None

        matches = re.findall(
            r"The Dart VM service is listening on http://127\.0\.0\.1:(\d+)/",
            log_output,
        )
        if not matches:
            return None

        return int(matches[-1])

    def _run_attach_and_trigger_action(
        self,
        project_dir: str,
        package_name: str,
        action: str,
        target: str = "lib/main.dart",
        flutter_executable: str = "flutter",
        debug_port: int | None = None,
        debug_url: str | None = None,
        additional_args: str | None = None,
        attach_wait_seconds: int = 20,
        action_wait_seconds: int = 4,
    ) -> str:
        """
        Attach to an already-running Flutter app (typically launched by VS Code),
        trigger hot reload/restart, and detach.
        """
        if action not in {"reload", "restart"}:
            raise RuntimeError("action must be 'reload' or 'restart'.")
        if not os.path.isdir(project_dir):
            raise RuntimeError(f"Project directory not found: {project_dir}")

        if not debug_url and (debug_port is None or debug_port <= 0):
            discovered_url = self._discover_vm_service_debug_url(package_name)
            if not discovered_url:
                discovered_url = self._discover_vm_service_debug_url_from_host()
            if discovered_url:
                debug_url = discovered_url
            else:
                discovered_port = self._discover_vm_service_port(package_name)
                if discovered_port is None:
                    raise RuntimeError(
                        "Could not discover Dart VM service endpoint from logcat. "
                        "Pass debug_url/debug_port explicitly."
                    )
                debug_port = discovered_port

        flutter_cmd = self._resolve_flutter_executable(flutter_executable)
        command = [
            flutter_cmd,
            "attach",
            "-d",
            self.device_serial,
            "--app-id",
            package_name,
            "-t",
            target,
            "--no-dds",
        ]

        if debug_url:
            command.extend(["--debug-url", debug_url])
        elif debug_port:
            command.extend(["--debug-port", str(debug_port)])

        if additional_args:
            command.extend(shlex.split(additional_args, posix=os.name != "nt"))

        attach_log_path = os.path.join(project_dir, ".mcp_flutter_attach.log")
        with open(attach_log_path, "w", encoding="utf-8") as log_handle:
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            proc = subprocess.Popen(
                command,
                cwd=project_dir,
                stdin=subprocess.PIPE,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creationflags,
            )

            connected_patterns = (
                "A Dart VM Service on",
                "Connected to _flutterView",
                "The Flutter DevTools debugger and profiler",
            )
            deadline = time.time() + max(1, attach_wait_seconds)
            ready = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    tail = self._tail_file(attach_log_path, 120)
                    return (
                        f"flutter attach exited early with code {proc.returncode}.\n"
                        f"Log tail:\n{tail}"
                    )

                tail = self._tail_file(attach_log_path, 120)
                if any(pattern in tail for pattern in connected_patterns):
                    ready = True
                    break

                time.sleep(0.5)

            if not ready:
                proc.kill()
                proc.wait(timeout=5)
                tail = self._tail_file(attach_log_path, 120)
                return (
                    "flutter attach did not become ready before timeout.\n"
                    f"Attach log: {attach_log_path}\n"
                    f"Log tail:\n{tail}"
                )

            if not proc.stdin:
                proc.kill()
                proc.wait(timeout=5)
                tail = self._tail_file(attach_log_path, 120)
                return (
                    "flutter attach started but stdin is unavailable; cannot trigger action.\n"
                    f"Log tail:\n{tail}"
                )

            key = "r" if action == "reload" else "R"
            proc.stdin.write(f"{key}\n")
            proc.stdin.flush()

            action_deadline = time.time() + max(1, action_wait_seconds)
            action_patterns = (
                "Performing hot reload",
                "Reloaded ",
            ) if action == "reload" else (
                "Performing hot restart",
                "Restarted application",
            )
            while time.time() < action_deadline:
                tail = self._tail_file(attach_log_path, 120)
                if any(pattern in tail for pattern in action_patterns):
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.25)

            # Detach from attach session.
            proc.stdin.write("q\n")
            proc.stdin.flush()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

        tail = self._tail_file(attach_log_path, 120)
        port_msg = f"debug_port={debug_port}" if debug_port else "debug_url used"
        confirmation = ""
        if "Reloaded " not in tail and "Restarted application" not in tail:
            confirmation = "\nAction confirmation not observed in attach log; reload/restart may not have applied."
        return (
            f"Triggered hot {action} via flutter attach against {package_name} ({port_msg}).\n"
            f"Attach log: {attach_log_path}\n"
            f"Log tail:\n{tail}{confirmation}"
        )

    def hot_reload_vscode_session(
        self,
        project_dir: str,
        package_name: str,
        target: str = "lib/main.dart",
        flutter_executable: str = "flutter",
        debug_port: int | None = None,
        debug_url: str | None = None,
        additional_args: str | None = None,
        attach_wait_seconds: int = 20,
        action_wait_seconds: int = 4,
    ) -> str:
        """One-shot hot reload for a Flutter app already running from VS Code/IDE."""
        return self._run_attach_and_trigger_action(
            project_dir=project_dir,
            package_name=package_name,
            action="reload",
            target=target,
            flutter_executable=flutter_executable,
            debug_port=debug_port,
            debug_url=debug_url,
            additional_args=additional_args,
            attach_wait_seconds=attach_wait_seconds,
            action_wait_seconds=action_wait_seconds,
        )

    def hot_restart_vscode_session(
        self,
        project_dir: str,
        package_name: str,
        target: str = "lib/main.dart",
        flutter_executable: str = "flutter",
        debug_port: int | None = None,
        debug_url: str | None = None,
        additional_args: str | None = None,
        attach_wait_seconds: int = 20,
        action_wait_seconds: int = 4,
    ) -> str:
        """One-shot hot restart for a Flutter app already running from VS Code/IDE."""
        return self._run_attach_and_trigger_action(
            project_dir=project_dir,
            package_name=package_name,
            action="restart",
            target=target,
            flutter_executable=flutter_executable,
            debug_port=debug_port,
            debug_url=debug_url,
            additional_args=additional_args,
            attach_wait_seconds=attach_wait_seconds,
            action_wait_seconds=action_wait_seconds,
        )

    def _capture_raw_screenshot(self, output_path: str) -> None:
        """Capture a full-resolution screenshot from the device."""
        host_path = Path(output_path)
        host_path.parent.mkdir(parents=True, exist_ok=True)

        device_path = "/sdcard/mcp_screenshot.png"
        self.device.shell(f"screencap -p {device_path}")
        self.device.pull(device_path, str(host_path))
        self.device.shell(f"rm {device_path}")

    def _download_binary_file(self, url: str, output_path: str, headers: dict[str, str] | None = None) -> None:
        req = urlrequest.Request(url, headers=headers or {})
        with urlrequest.urlopen(req, timeout=30) as response:
            body = response.read()
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)

    def _resolve_figma_token(self, figma_token: str | None) -> str:
        token = (figma_token or os.getenv("FIGMA_TOKEN") or "").strip()
        if not token:
            raise RuntimeError(
                "Figma token is required. Pass figma_token or set FIGMA_TOKEN env var."
            )
        return token

    def _get_figma_node_image_url(
        self,
        file_key: str,
        node_id: str,
        figma_token: str,
        scale: float,
        use_absolute_bounds: bool,
    ) -> str:
        quoted_file_key = urlparse.quote(file_key, safe="")
        query = urlparse.urlencode(
            {
                "ids": node_id,
                "format": "png",
                "scale": str(scale),
                "use_absolute_bounds": "true" if use_absolute_bounds else "false",
            }
        )
        api_url = f"https://api.figma.com/v1/images/{quoted_file_key}?{query}"
        req = urlrequest.Request(
            api_url,
            headers={"X-Figma-Token": figma_token},
        )

        try:
            with urlrequest.urlopen(req, timeout=30) as response:
                payload = response.read().decode("utf-8")
        except urlerror.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Figma API request failed ({exc.code}): {body}") from exc
        except urlerror.URLError as exc:
            raise RuntimeError(f"Figma API request failed: {exc}") from exc

        import json

        parsed = json.loads(payload)
        image_url = (parsed.get("images") or {}).get(node_id)
        if not image_url:
            err_msg = parsed.get("err")
            if err_msg:
                raise RuntimeError(f"Figma did not return an image URL: {err_msg}")
            raise RuntimeError("Figma did not return an image URL for the requested node.")

        return image_url

    @staticmethod
    def _coarse_mae(
        fig_img: PILImage.Image,
        emu_scaled: PILImage.Image,
        y_offset: int,
        step: int = 2,
    ) -> float:
        fig_px = fig_img.load()
        emu_px = emu_scaled.load()
        width, height = fig_img.size

        total = 0.0
        count = 0
        for y in range(0, height, step):
            source_y = y + y_offset
            for x in range(0, width, step):
                r1, g1, b1 = fig_px[x, y]
                r2, g2, b2 = emu_px[x, source_y]
                total += abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
                count += 3

        if count == 0:
            return 255.0
        return total / count

    def compare_screen_with_figma(
        self,
        file_key: str,
        node_id: str,
        figma_token: str | None = None,
        scale: float = 1.0,
        use_absolute_bounds: bool = True,
        grid_cols: int = 6,
        grid_rows: int = 10,
        output_dir: str = ".mcp_pixel_diff",
    ) -> dict:
        """
        Capture the current emulator screen and compare it pixel-by-pixel against a Figma node export.

        Returns a dictionary with alignment details, image-diff metrics, zone scores,
        worst grid cells, and generated artifact paths.
        """
        if not file_key.strip():
            raise RuntimeError("file_key is required.")
        if not node_id.strip():
            raise RuntimeError("node_id is required.")
        if scale <= 0:
            raise RuntimeError("scale must be > 0.")
        if grid_cols <= 0 or grid_rows <= 0:
            raise RuntimeError("grid_cols and grid_rows must be > 0.")

        token = self._resolve_figma_token(figma_token)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        emulator_raw = out_dir / f"{run_id}_emulator_raw.png"
        figma_node_path = out_dir / f"{run_id}_figma_node.png"
        emulator_scaled_path = out_dir / f"{run_id}_emulator_scaled.png"
        emulator_aligned_path = out_dir / f"{run_id}_emulator_aligned.png"
        heatmap_path = out_dir / f"{run_id}_heatmap.png"

        self._capture_raw_screenshot(str(emulator_raw))

        figma_image_url = self._get_figma_node_image_url(
            file_key=file_key,
            node_id=node_id,
            figma_token=token,
            scale=scale,
            use_absolute_bounds=use_absolute_bounds,
        )
        self._download_binary_file(figma_image_url, str(figma_node_path))

        fig_img = PILImage.open(figma_node_path).convert("RGB")
        emu_raw_img = PILImage.open(emulator_raw).convert("RGB")

        target_w, target_h = fig_img.size
        scaled_h = int(round(emu_raw_img.height * target_w / emu_raw_img.width))
        if scaled_h < target_h:
            raise RuntimeError(
                f"Scaled emulator image is shorter than Figma image ({scaled_h} < {target_h})."
            )

        emu_scaled = emu_raw_img.resize(
            (target_w, scaled_h),
            PILImage.Resampling.LANCZOS,
        )
        emu_scaled.save(emulator_scaled_path, format="PNG")

        best_y = 0
        best_mae = float("inf")
        max_offset = scaled_h - target_h
        for y_offset in range(max_offset + 1):
            score = self._coarse_mae(fig_img, emu_scaled, y_offset, step=2)
            if score < best_mae:
                best_mae = score
                best_y = y_offset

        emu_aligned = emu_scaled.crop((0, best_y, target_w, best_y + target_h))
        emu_aligned.save(emulator_aligned_path, format="PNG")

        fig_px = fig_img.load()
        emu_px = emu_aligned.load()
        heatmap = PILImage.new("RGB", (target_w, target_h))
        heat_px = heatmap.load()

        channels = target_w * target_h * 3
        px_total = target_w * target_h
        sum_abs = 0.0
        sum_sq = 0.0
        diff_gt_10 = 0
        diff_gt_25 = 0
        diff_gt_50 = 0

        cell_count = grid_cols * grid_rows
        cell_sums = [0.0] * cell_count
        cell_pixels = [0] * cell_count

        zones = [
            {"name": "header", "y0": 0.00, "y1": 0.24, "sum": 0.0, "count": 0},
            {"name": "cards", "y0": 0.24, "y1": 0.47, "sum": 0.0, "count": 0},
            {"name": "gap_analysis", "y0": 0.47, "y1": 0.72, "sum": 0.0, "count": 0},
            {"name": "strategy_band", "y0": 0.72, "y1": 0.87, "sum": 0.0, "count": 0},
            {"name": "bottom_nav", "y0": 0.87, "y1": 1.00, "sum": 0.0, "count": 0},
        ]

        for y in range(target_h):
            y_norm = y / target_h
            for x in range(target_w):
                r1, g1, b1 = fig_px[x, y]
                r2, g2, b2 = emu_px[x, y]

                dr = abs(r1 - r2)
                dg = abs(g1 - g2)
                db = abs(b1 - b2)

                sum_abs += dr + dg + db
                sum_sq += (dr * dr) + (dg * dg) + (db * db)

                d_avg = (dr + dg + db) / 3.0
                if d_avg > 10:
                    diff_gt_10 += 1
                if d_avg > 25:
                    diff_gt_25 += 1
                if d_avg > 50:
                    diff_gt_50 += 1

                cx = min(grid_cols - 1, (x * grid_cols) // target_w)
                cy = min(grid_rows - 1, (y * grid_rows) // target_h)
                idx = (cy * grid_cols) + cx
                cell_sums[idx] += d_avg
                cell_pixels[idx] += 1

                for zone in zones:
                    if zone["y0"] <= y_norm < zone["y1"]:
                        zone["sum"] += d_avg
                        zone["count"] += 1
                        break

                heat_val = min(255, int(round(d_avg * 4)))
                heat_px[x, y] = (heat_val, 0, 0)

        heatmap.save(heatmap_path, format="PNG")

        mae = sum_abs / channels
        rmse = (sum_sq / channels) ** 0.5
        similarity_pct = max(0.0, 100.0 - ((mae / 255.0) * 100.0))

        grid_cells = []
        for row in range(grid_rows):
            for col in range(grid_cols):
                idx = row * grid_cols + col
                avg = (cell_sums[idx] / cell_pixels[idx]) if cell_pixels[idx] else 0.0
                grid_cells.append(
                    {
                        "row": row,
                        "col": col,
                        "avgDiff": round(avg, 3),
                        "similarityPct": round(max(0.0, 100.0 - ((avg / 255.0) * 100.0)), 3),
                    }
                )

        zone_report = []
        for zone in zones:
            avg = (zone["sum"] / zone["count"]) if zone["count"] else 0.0
            zone_report.append(
                {
                    "name": zone["name"],
                    "avgDiff": round(avg, 3),
                    "similarityPct": round(max(0.0, 100.0 - ((avg / 255.0) * 100.0)), 3),
                }
            )

        worst_grid_cells = sorted(grid_cells, key=lambda cell: cell["avgDiff"], reverse=True)[:12]

        return {
            "figma": {
                "fileKey": file_key,
                "nodeId": node_id,
                "scale": scale,
                "width": target_w,
                "height": target_h,
                "useAbsoluteBounds": use_absolute_bounds,
            },
            "alignment": {
                "scaledWidth": target_w,
                "scaledHeight": scaled_h,
                "bestYOffset": best_y,
                "coarseMaeAtBestOffset": round(best_mae, 4),
            },
            "metrics": {
                "mae": round(mae, 4),
                "rmse": round(rmse, 4),
                "similarityPct": round(similarity_pct, 4),
                "pxDiffGt10Pct": round((diff_gt_10 * 100.0) / px_total, 4),
                "pxDiffGt25Pct": round((diff_gt_25 * 100.0) / px_total, 4),
                "pxDiffGt50Pct": round((diff_gt_50 * 100.0) / px_total, 4),
            },
            "zones": zone_report,
            "worstGridCells": worst_grid_cells,
            "artifacts": {
                "emulatorRaw": str(emulator_raw),
                "figmaNode": str(figma_node_path),
                "emulatorScaled": str(emulator_scaled_path),
                "emulatorAligned": str(emulator_aligned_path),
                "heatmap": str(heatmap_path),
            },
        }

    def take_screenshot(self) -> None:
        self._capture_raw_screenshot("screenshot.png")

        # Compress screenshot to avoid client-side payload issues.
        with PILImage.open("screenshot.png") as img:
            width, height = img.size
            new_width = int(width * 0.3)
            new_height = int(height * 0.3)
            resized_img = img.resize((new_width, new_height), PILImage.Resampling.LANCZOS)
            resized_img.save("compressed_screenshot.png", "PNG", quality=85, optimize=True)

    def get_uilayout(self) -> str:
        self.device.shell("uiautomator dump")
        self.device.pull("/sdcard/window_dump.xml", "window_dump.xml")
        self.device.shell("rm /sdcard/window_dump.xml")

        import re
        import xml.etree.ElementTree as ET

        def calculate_center(bounds_str):
            matches = re.findall(r"\[(\d+),(\d+)\]", bounds_str)
            if len(matches) == 2:
                x1, y1 = map(int, matches[0])
                x2, y2 = map(int, matches[1])
                center_x = (x1 + x2) // 2
                center_y = (y1 + y2) // 2
                return center_x, center_y
            return None

        tree = ET.parse("window_dump.xml")
        root = tree.getroot()

        clickable_elements = []
        for element in root.findall(".//node[@clickable='true']"):
            text = element.get("text", "")
            content_desc = element.get("content-desc", "")
            bounds = element.get("bounds", "")

            # Only include elements that have either text or content description
            if text or content_desc:
                center = calculate_center(bounds)
                element_info = "Clickable element:"
                if text:
                    element_info += f"\n  Text: {text}"
                if content_desc:
                    element_info += f"\n  Description: {content_desc}"
                element_info += f"\n  Bounds: {bounds}"
                if center:
                    element_info += f"\n  Center: ({center[0]}, {center[1]})"
                clickable_elements.append(element_info)

        if not clickable_elements:
            return "No clickable elements found with text or description"
        else:
            result = "\n\n".join(clickable_elements)
            return result
