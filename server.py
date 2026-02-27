import os
import sys

import yaml
from mcp.server.fastmcp import FastMCP, Image

from adbdevicemanager import AdbDeviceManager

CONFIG_FILE = "config.yaml"
CONFIG_FILE_EXAMPLE = "config.yaml.example"

# Load config (make config file optional)
config = {}
device_name = None

if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE) as f:
            config = yaml.safe_load(f.read()) or {}
        device_config = config.get("device", {})
        configured_device_name = device_config.get(
            "name") if device_config else None

        # Support multiple ways to specify auto-selection:
        # 1. name: null (None in Python)
        # 2. name: "" (empty string)
        # 3. name field completely missing
        if configured_device_name and configured_device_name.strip():
            device_name = configured_device_name.strip()
            print(f"Loaded config from {CONFIG_FILE}")
            print(f"Configured device: {device_name}")
        else:
            print(f"Loaded config from {CONFIG_FILE}")
            print(
                "No device specified in config, will auto-select if only one device connected")
    except Exception as e:
        print(f"Error loading config file {CONFIG_FILE}: {e}", file=sys.stderr)
        print(
            f"Please check the format of your config file or recreate it from {CONFIG_FILE_EXAMPLE}", file=sys.stderr)
        sys.exit(1)
else:
    print(
        f"Config file {CONFIG_FILE} not found, using auto-selection for device")

# Initialize MCP and device manager
# AdbDeviceManager will handle auto-selection if device_name is None
mcp = FastMCP("android")
deviceManager = AdbDeviceManager(device_name)


@mcp.tool()
def get_packages() -> str:
    """
    Get all installed packages on the device
    Returns:
        str: A list of all installed packages on the device as a string
    """
    result = deviceManager.get_packages()
    return result


@mcp.tool()
def execute_adb_shell_command(command: str) -> str:
    """Executes an ADB command and returns the output or an error.
    Args:
        command (str): The ADB shell command to execute
    Returns:
        str: The output of the ADB command
    """
    result = deviceManager.execute_adb_shell_command(command)
    return result


@mcp.tool()
def get_uilayout() -> str:
    """
    Retrieves information about clickable elements in the current UI.
    Returns a formatted string containing details about each clickable element,
    including its text, content description, bounds, and center coordinates.

    Returns:
        str: A formatted list of clickable elements with their properties
    """
    result = deviceManager.get_uilayout()
    return result


@mcp.tool()
def get_screenshot() -> Image:
    """Takes a screenshot of the device and returns it.
    Returns:
        Image: the screenshot
    """
    deviceManager.take_screenshot()
    return Image(path="compressed_screenshot.png")


@mcp.tool()
def get_package_action_intents(package_name: str) -> list[str]:
    """
    Get all non-data actions from Activity Resolver Table for a package
    Args:
        package_name (str): The name of the package to get actions for
    Returns:
        list[str]: A list of all non-data actions from the Activity Resolver Table for the package
    """
    result = deviceManager.get_package_action_intents(package_name)
    return result


@mcp.tool()
def launch_app(package_name: str, activity_name: str | None = None, stop_first: bool = False) -> str:
    """
    Launch an Android app on the connected device.
    Args:
        package_name (str): Android package name (for example, com.example.app)
        activity_name (str | None): Optional activity name or full component
        stop_first (bool): Force-stop the package before launch
    Returns:
        str: ADB launch output
    """
    return deviceManager.launch_app(
        package_name=package_name,
        activity_name=activity_name,
        stop_first=stop_first,
    )


@mcp.tool()
def start_flutter_run(
    project_dir: str,
    target: str = "lib/main.dart",
    flutter_executable: str = "flutter",
    additional_args: str | None = None,
    startup_wait_seconds: int = 8,
) -> str:
    """
    Start a managed `flutter run` session tied to the selected device.
    Args:
        project_dir (str): Flutter project directory on the host machine
        target (str): Flutter target entrypoint (defaults to lib/main.dart)
        flutter_executable (str): Flutter binary name/path on host
        additional_args (str | None): Extra args appended to flutter run
        startup_wait_seconds (int): Seconds to wait before startup check
    Returns:
        str: Startup status and log location
    """
    return deviceManager.start_flutter_run(
        project_dir=project_dir,
        target=target,
        flutter_executable=flutter_executable,
        additional_args=additional_args,
        startup_wait_seconds=startup_wait_seconds,
    )


@mcp.tool()
def hot_reload_flutter_run() -> str:
    """
    Trigger hot reload for the managed flutter run process.
    Returns:
        str: Operation status
    """
    return deviceManager.hot_reload_flutter_run()


@mcp.tool()
def hot_restart_flutter_run() -> str:
    """
    Trigger hot restart for the managed flutter run process.
    Returns:
        str: Operation status
    """
    return deviceManager.hot_restart_flutter_run()


@mcp.tool()
def stop_flutter_run(graceful_wait_seconds: int = 10) -> str:
    """
    Stop the managed flutter run process started by start_flutter_run.
    Args:
        graceful_wait_seconds (int): Wait time before force kill fallback
    Returns:
        str: Stop status
    """
    return deviceManager.stop_flutter_run(graceful_wait_seconds=graceful_wait_seconds)


@mcp.tool()
def get_flutter_run_log(lines: int = 60) -> str:
    """
    Read the tail of the managed flutter run log.
    Args:
        lines (int): Number of lines to read from the end of log
    Returns:
        str: Log tail
    """
    return deviceManager.get_flutter_run_log(lines=lines)


@mcp.tool()
def hot_reload_vscode_session(
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
    """
    Attach to an app already running from VS Code/IDE and trigger a one-shot hot reload.

    Args:
        project_dir (str): Flutter project directory on host
        package_name (str): Android package name (for example, com.example.app)
        target (str): Flutter target entrypoint
        flutter_executable (str): Flutter binary name/path on host
        debug_port (int | None): Optional VM service port (auto-discovered if omitted)
        debug_url (str | None): Optional VM service URL (overrides debug_port)
        additional_args (str | None): Additional args appended to `flutter attach`
        attach_wait_seconds (int): Wait time for attach to stabilize
        action_wait_seconds (int): Wait time after triggering reload before detach

    Returns:
        str: Attach/reload outcome and log tail
    """
    return deviceManager.hot_reload_vscode_session(
        project_dir=project_dir,
        package_name=package_name,
        target=target,
        flutter_executable=flutter_executable,
        debug_port=debug_port,
        debug_url=debug_url,
        additional_args=additional_args,
        attach_wait_seconds=attach_wait_seconds,
        action_wait_seconds=action_wait_seconds,
    )


@mcp.tool()
def hot_restart_vscode_session(
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
    """
    Attach to an app already running from VS Code/IDE and trigger a one-shot hot restart.

    Args:
        project_dir (str): Flutter project directory on host
        package_name (str): Android package name (for example, com.example.app)
        target (str): Flutter target entrypoint
        flutter_executable (str): Flutter binary name/path on host
        debug_port (int | None): Optional VM service port (auto-discovered if omitted)
        debug_url (str | None): Optional VM service URL (overrides debug_port)
        additional_args (str | None): Additional args appended to `flutter attach`
        attach_wait_seconds (int): Wait time for attach to stabilize
        action_wait_seconds (int): Wait time after triggering restart before detach

    Returns:
        str: Attach/restart outcome and log tail
    """
    return deviceManager.hot_restart_vscode_session(
        project_dir=project_dir,
        package_name=package_name,
        target=target,
        flutter_executable=flutter_executable,
        debug_port=debug_port,
        debug_url=debug_url,
        additional_args=additional_args,
        attach_wait_seconds=attach_wait_seconds,
        action_wait_seconds=action_wait_seconds,
    )


@mcp.tool()
def compare_screen_with_figma(
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
    Pixel-compare current device screen against a Figma node export.

    Args:
        file_key (str): Figma file key
        node_id (str): Figma node id (example: "21:1074")
        figma_token (str | None): Optional Figma token. If omitted, uses FIGMA_TOKEN env var
        scale (float): Figma export scale (supports 3.0 for "3x" export)
        use_absolute_bounds (bool): Whether to request absolute bounds from Figma export API
        grid_cols (int): Fragmentation columns for grid analysis
        grid_rows (int): Fragmentation rows for grid analysis
        output_dir (str): Host directory for generated artifacts

    Returns:
        dict: Diff report with metrics, zones, worst cells, and artifact paths
    """
    return deviceManager.compare_screen_with_figma(
        file_key=file_key,
        node_id=node_id,
        figma_token=figma_token,
        scale=scale,
        use_absolute_bounds=use_absolute_bounds,
        grid_cols=grid_cols,
        grid_rows=grid_rows,
        output_dir=output_dir,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
