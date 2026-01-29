import logging
import shlex
import subprocess
import time
import uuid
from typing import Any

from typing_extensions import Self

from swerex import PACKAGE_NAME, REMOTE_EXECUTABLE_NAME
from swerex.deployment.abstract import AbstractDeployment
from swerex.deployment.config import DockerDeploymentConfig
from swerex.deployment.hooks.abstract import CombinedDeploymentHook, DeploymentHook
from swerex.exceptions import DeploymentNotStartedError, DockerPullError
from swerex.runtime.abstract import IsAliveResponse
from swerex.runtime.config import RemoteRuntimeConfig
from swerex.runtime.remote import RemoteRuntime
from swerex.utils.free_port import find_free_port
from swerex.utils.log import get_logger
from swerex.utils.wait import _wait_until_alive

__all__ = ["DockerDeployment", "DockerDeploymentConfig"]


def _is_image_available(image: str, runtime: str = "docker") -> bool:
    """Check if an image is available locally.
    
    For docker/podman: checks if the image exists in local registry.
    For apptainer: checks if the .sif file exists.
    """
    if runtime == "apptainer":
        # For apptainer, check if the .sif file exists
        # Image might be docker://ubuntu:latest or path/to/image.sif
        if image.endswith(".sif"):
            from pathlib import Path
            return Path(image).exists()
        # If it's a docker:// URI, we need to check the cached .sif
        # Apptainer caches in ~/.apptainer/cache or APPTAINER_CACHEDIR
        # For now, return False to trigger a pull
        return False
    
    try:
        subprocess.check_call(
            [runtime, "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _pull_image(image: str, runtime: str = "docker") -> bytes:
    """Pull an image.
    
    For docker/podman: pulls from registry.
    For apptainer: pulls and converts to .sif format.
    """
    if runtime == "apptainer":
        # Apptainer can pull from docker://, library://, shub://, etc.
        # If no protocol, assume docker://
        if not any(image.startswith(proto) for proto in ["docker://", "library://", "shub://", "oras://"]):
            if not image.endswith(".sif"):
                image = f"docker://{image}"
        
        try:
            return subprocess.check_output([runtime, "pull", image], stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            raise subprocess.CalledProcessError(e.returncode, e.cmd, e.output, e.stderr) from None
    
    try:
        return subprocess.check_output([runtime, "pull", image], stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        # e.stderr contains the error message as bytes
        raise subprocess.CalledProcessError(e.returncode, e.cmd, e.output, e.stderr) from None


def _remove_image(image: str, runtime: str = "docker") -> bytes:
    """Remove an image.
    
    For docker/podman: removes from local registry.
    For apptainer: removes the .sif file if it's a local file.
    """
    if runtime == "apptainer":
        # For apptainer, if it's a .sif file, just remove it
        if image.endswith(".sif"):
            from pathlib import Path
            path = Path(image)
            if path.exists():
                path.unlink()
                return b"Removed " + image.encode()
        # For docker:// URIs, the .sif is in cache, harder to find
        # Skip for now as it's not critical
        return b"Apptainer image removal not fully implemented for URIs"
    
    return subprocess.check_output([runtime, "rmi", image], timeout=30)


class DockerDeployment(AbstractDeployment):
    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        **kwargs: Any,
    ):
        """Deployment to local container image using Docker or Podman.

        Args:
            **kwargs: Keyword arguments (see `DockerDeploymentConfig` for details).
        """
        self._config = DockerDeploymentConfig(**kwargs)
        self._runtime: RemoteRuntime | None = None
        self._container_process = None
        self._container_name = None
        self.logger = logger or get_logger("rex-deploy")
        self._runtime_timeout = 0.15
        self._hooks = CombinedDeploymentHook()

    def add_hook(self, hook: DeploymentHook):
        self._hooks.add_hook(hook)

    @classmethod
    def from_config(cls, config: DockerDeploymentConfig) -> Self:
        return cls(**config.model_dump())

    def _get_container_name(self) -> str:
        """Returns a unique container name based on the image name."""
        image_name_sanitized = "".join(c for c in self._config.image if c.isalnum() or c in "-_.")
        return f"{image_name_sanitized}-{uuid.uuid4()}"
    
    def _get_sif_image_path(self, image: str) -> str:
        """Convert a docker image reference to a .sif file path for Apptainer.
        
        Args:
            image: Docker image reference like "ubuntu:latest" or "docker://ubuntu:latest"
            
        Returns:
            Path to the .sif file that apptainer pull will create
        """
        # Remove docker:// prefix if present
        clean_image = image.replace("docker://", "").replace("library://", "").replace(":", "_").replace("/", "_")
        if not clean_image.endswith(".sif"):
            clean_image = f"{clean_image}.sif"
        
        # Use cache dir if specified
        if self._config.apptainer_sif_cache_dir:
            from pathlib import Path
            cache_dir = Path(self._config.apptainer_sif_cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            return str(cache_dir / clean_image)
        
        return clean_image

    @property
    def container_name(self) -> str | None:
        return self._container_name

    async def is_alive(self, *, timeout: float | None = None) -> IsAliveResponse:
        """Checks if the runtime is alive. The return value can be
        tested with bool().

        Raises:
            DeploymentNotStartedError: If the deployment was not started.
        """
        if self._runtime is None:
            msg = "Runtime not started"
            raise RuntimeError(msg)
        if self._container_process is None:
            msg = "Container process not started"
            raise RuntimeError(msg)
        if self._container_process.poll() is not None:
            msg = "Container process terminated."
            output = "stdout:\n" + self._container_process.stdout.read().decode()  # type: ignore
            output += "\nstderr:\n" + self._container_process.stderr.read().decode()  # type: ignore
            msg += "\n" + output
            raise RuntimeError(msg)
        return await self._runtime.is_alive(timeout=timeout)

    async def _wait_until_alive(self, timeout: float = 10.0):
        try:
            return await _wait_until_alive(self.is_alive, timeout=timeout, function_timeout=self._runtime_timeout)
        except TimeoutError as e:
            self.logger.error("Runtime did not start within timeout. Here's the output from the container process.")
            self.logger.error(self._container_process.stdout.read().decode())  # type: ignore
            self.logger.error(self._container_process.stderr.read().decode())  # type: ignore
            assert self._container_process is not None
            await self.stop()
            raise e

    def _get_token(self) -> str:
        return str(uuid.uuid4())

    def _get_swerex_start_cmd(self, token: str, port: int | None = None) -> list[str]:
        """Get the command to start the swerex server.
        
        Args:
            token: Authentication token
            port: Port to run server on (for apptainer with host networking)
        """
        rex_args = f"--auth-token {token}"
        
        # For Apptainer with host networking, we need to specify the port
        if port is not None and self._config.container_runtime == "apptainer":
            rex_args += f" --port {port}"
        
        pipx_install = "python3 -m pip install pipx && python3 -m pipx ensurepath"
        if self._config.python_standalone_dir:
            cmd = f"{self._config.python_standalone_dir}/python3.11/bin/{REMOTE_EXECUTABLE_NAME} {rex_args}"
        else:
            cmd = f"{REMOTE_EXECUTABLE_NAME} {rex_args} || ({pipx_install} && pipx run {PACKAGE_NAME} {rex_args})"
        # Use exec_shell from config
        return [
            *self._config.exec_shell,
            cmd,
        ]

    def _pull_image(self) -> None:
        """Pull container image if needed.
        
        For apptainer, this converts the image to a .sif file.
        """
        if self._config.pull == "never":
            return
        
        runtime = self._config.container_runtime
        
        # For apptainer, check if .sif file exists
        if runtime == "apptainer":
            sif_path = self._get_sif_image_path(self._config.image)
            if self._config.pull == "missing":
                from pathlib import Path
                if Path(sif_path).exists():
                    self.logger.info(f"Using existing .sif file: {sif_path}")
                    return
        elif self._config.pull == "missing" and _is_image_available(self._config.image, runtime):
            return
            
        self.logger.info(f"Pulling image {self._config.image!r}")
        self._hooks.on_custom_step("Pulling container image")
        try:
            if runtime == "apptainer":
                # Pull to specific .sif file
                sif_path = self._get_sif_image_path(self._config.image)
                image_uri = self._config.image
                if not any(image_uri.startswith(p) for p in ["docker://", "library://", "shub://", "oras://"]):
                    image_uri = f"docker://{image_uri}"
                
                subprocess.check_output([runtime, "pull", sif_path, image_uri], stderr=subprocess.PIPE)
            else:
                _pull_image(self._config.image, runtime)
        except subprocess.CalledProcessError as e:
            msg = f"Failed to pull image {self._config.image}. "
            msg += f"Error: {e.stderr.decode()}"
            msg += f"Output: {e.output.decode()}"
            raise DockerPullError(msg) from e

    @property
    def glibc_dockerfile(self) -> str:
        # will only work with glibc-based systems
        if self._config.platform:
            platform_arg = f"--platform={self._config.platform}"
        else:
            platform_arg = ""
        return (
            "ARG BASE_IMAGE\n\n"
            # Build stage for standalone Python
            f"FROM {platform_arg} python:3.11.9-slim-bookworm AS builder\n"
            # Install build dependencies
            "RUN apt-get update && apt-get install -y \\\n"
            "    wget \\\n"
            "    gcc \\\n"
            "    make \\\n"
            "    zlib1g-dev \\\n"
            "    libssl-dev \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
            # Download and compile Python as standalone
            "WORKDIR /build\n"
            "RUN wget https://www.python.org/ftp/python/3.11.8/Python-3.11.8.tgz \\\n"
            "    && tar xzf Python-3.11.8.tgz\n"
            "WORKDIR /build/Python-3.11.8\n"
            "RUN ./configure \\\n"
            "    --prefix=/root/python3.11 \\\n"
            "    --enable-shared \\\n"
            "    LDFLAGS='-Wl,-rpath=/root/python3.11/lib' && \\\n"
            "    make -j$(nproc) && \\\n"
            "    make install && \\\n"
            "    ldconfig\n\n"
            # Production stage
            f"FROM {platform_arg} $BASE_IMAGE\n"
            # Ensure we have the required runtime libraries
            "RUN apt-get update && apt-get install -y \\\n"
            "    libc6 \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
            # Copy the standalone Python installation
            f"COPY --from=builder /root/python3.11 {self._config.python_standalone_dir}/python3.11\n"
            f"ENV LD_LIBRARY_PATH={self._config.python_standalone_dir}/python3.11/lib:${{LD_LIBRARY_PATH:-}}\n"
            # Verify installation
            f"RUN {self._config.python_standalone_dir}/python3.11/bin/python3 --version\n"
            # Install swe-rex using the standalone Python
            f"RUN /root/python3.11/bin/pip3 install --no-cache-dir {PACKAGE_NAME}\n\n"
            f"RUN ln -s /root/python3.11/bin/{REMOTE_EXECUTABLE_NAME} /usr/local/bin/{REMOTE_EXECUTABLE_NAME}\n\n"
            f"RUN {REMOTE_EXECUTABLE_NAME} --version\n"
        )

    def _build_image(self) -> str:
        """Builds image, returns image ID/path.
        
        For docker/podman: returns image ID (sha256:...)
        For apptainer: returns path to .sif file
        """
        runtime = self._config.container_runtime
        
        self.logger.info(
            f"Building image {self._config.image} to install a standalone python to {self._config.python_standalone_dir}. "
            "This might take a while (but you only have to do it once). To skip this step, set `python_standalone_dir` to None."
        )
        
        if runtime == "apptainer":
            # For Apptainer, we need to create a definition file
            # First, build a docker image, convert to .sif
            self.logger.info("Building with Apptainer requires building Docker image first, then converting")
            
            # Build with docker first (if available)
            try:
                subprocess.check_output(["docker", "--version"], stderr=subprocess.DEVNULL)
                has_docker = True
            except (subprocess.CalledProcessError, FileNotFoundError):
                has_docker = False
            
            if not has_docker:
                msg = "Building with python_standalone_dir requires Docker to be available for initial build. "
                msg += "Please either: 1) Install Docker, 2) Build the image elsewhere and copy the .sif file, "
                msg += "or 3) Use an image that already has swe-rex installed."
                raise RuntimeError(msg)
            
            # Build docker image
            dockerfile = self.glibc_dockerfile
            platform_arg = []
            if self._config.platform:
                platform_arg = ["--platform", self._config.platform]
            
            build_cmd = [
                "docker",
                "build",
                "-q",
                *platform_arg,
                "--build-arg",
                f"BASE_IMAGE={self._config.image}",
                "-t",
                "swerex-apptainer-temp",
                "-",
            ]
            
            subprocess.check_output(build_cmd, input=dockerfile.encode())
            
            # Convert to .sif
            sif_path = self._get_sif_image_path(self._config.image + "-standalone")
            self.logger.info(f"Converting Docker image to Apptainer .sif at {sif_path}")
            subprocess.check_output(
                ["apptainer", "build", sif_path, "docker-daemon://swerex-apptainer-temp:latest"],
                stderr=subprocess.PIPE
            )
            
            # Clean up docker image
            try:
                subprocess.check_output(["docker", "rmi", "swerex-apptainer-temp"], stderr=subprocess.DEVNULL)
            except subprocess.CalledProcessError:
                pass
            
            return sif_path
        
        # Docker/Podman path
        dockerfile = self.glibc_dockerfile
        platform_arg = []
        if self._config.platform:
            platform_arg = ["--platform", self._config.platform]
        build_cmd = [
            runtime,
            "build",
            "-q",
            *platform_arg,
            "--build-arg",
            f"BASE_IMAGE={self._config.image}",
            "-",
        ]
        image_id = (
            subprocess.check_output(
                build_cmd,
                input=dockerfile.encode(),
            )
            .decode()
            .strip()
        )

        def is_valid_image_id(image_id):
            return image_id.startswith("sha256:") or (image_id.isalnum() and len(image_id) == 64)

        if not is_valid_image_id(image_id):
            msg = f"Failed to build image. Image ID is not a SHA256: {image_id}"
            raise RuntimeError(msg)
        return image_id

    async def start(self):
        """Starts the runtime."""
        self._pull_image()
        if self._config.python_standalone_dir:
            image_id = self._build_image()
        else:
            if self._config.container_runtime == "apptainer":
                image_id = self._get_sif_image_path(self._config.image)
            else:
                image_id = self._config.image
        
        if self._config.port is None:
            self._config.port = find_free_port()
        assert self._container_name is None
        self._container_name = self._get_container_name()
        token = self._get_token()
        
        runtime = self._config.container_runtime
        
        if runtime == "apptainer":
            # Apptainer: run SWE-ReX server as a long-lived process via `exec`
            # (no instances, no --net; host network is used by default).
            cmds = [
                runtime,
                "exec",
                "--writable-tmpfs",        # allow writes to /tmp in the container
                "--bind", "/tmp:/tmp",     # for any tmp/socket work
                *self._config.docker_args, # e.g. extra binds you configure in YAML
                image_id,                  # this is your .sif path or docker:// URI
                *self._get_swerex_start_cmd(
                    token,
                    port=self._config.port,
                ),
            ]

            cmd_str = shlex.join(cmds)
            self.logger.info(
                f"Starting Apptainer SWE-ReX server with image {self._config.image} "
                f"serving on port {self._config.port}"
            )
            self.logger.debug(f"Apptainer exec command: {cmd_str!r}")

            # Long-lived server process
            self._container_process = subprocess.Popen(
                cmds,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:
            # Docker/Podman path
            platform_arg = []
            if self._config.platform is not None:
                platform_arg = ["--platform", self._config.platform]
            rm_arg = []
            if self._config.remove_container:
                rm_arg = ["--rm"]
            cmds = [
                runtime,
                "run",
                *rm_arg,
                "-p",
                f"{self._config.port}:8000",
                *platform_arg,
                *self._config.docker_args,
                "--name",
                self._container_name,
                image_id,
                *self._get_swerex_start_cmd(token),
            ]
            cmd_str = shlex.join(cmds)
            self.logger.info(
                f"Starting container {self._container_name} with image {self._config.image} serving on port {self._config.port}"
            )
            self.logger.debug(f"Command: {cmd_str!r}")
            # shell=True required for && etc.
            self._container_process = subprocess.Popen(cmds, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        self._hooks.on_custom_step("Starting runtime")
        self.logger.info(f"Starting runtime at {self._config.port}")
        self._runtime = RemoteRuntime.from_config(
            RemoteRuntimeConfig(
                host=self._config.docker_internal_host,
                port=self._config.port,
                timeout=self._runtime_timeout,
                auth_token=token,
            )
        )
        t0 = time.time()
        await self._wait_until_alive(timeout=self._config.startup_timeout)
        self.logger.info(f"Runtime started in {time.time() - t0:.2f}s")

    async def stop(self):
        """Stops the runtime."""
        if self._runtime is not None:
            await self._runtime.close()
            self._runtime = None

        runtime = self._config.container_runtime
        
        if self._container_process is not None:
            if runtime == "apptainer":
                # Stop the Apptainer instance
                try:
                    subprocess.check_call(
                        [runtime, "instance", "stop", self._container_name],  # type: ignore
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    self.logger.warning(
                        f"Failed to stop Apptainer instance {self._container_name}: {e}. Will try harder.",
                        exc_info=False,
                    )
                    # Force stop
                    try:
                        subprocess.check_call(
                            [runtime, "instance", "stop", "-f", self._container_name],  # type: ignore
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                        )
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                        self.logger.warning(f"Failed to force stop Apptainer instance {self._container_name}")
            else:
                # Docker/Podman path
                try:
                    subprocess.check_call(
                        [runtime, "kill", self._container_name],  # type: ignore
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    self.logger.warning(
                        f"Failed to kill container {self._container_name}: {e}. Will try harder.",
                        exc_info=False,
                    )
            
            # Kill the process
            for _ in range(3):
                self._container_process.kill()
                try:
                    self._container_process.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
            else:
                self.logger.warning(f"Failed to kill container process for {self._container_name} with SIGKILL")

            self._container_process = None
            self._container_name = None

        if self._config.remove_images:
            if _is_image_available(self._config.image, runtime):
                self.logger.info(f"Removing image {self._config.image}")
                try:
                    _remove_image(self._config.image, runtime)
                except subprocess.CalledProcessError:
                    self.logger.error(f"Failed to remove image {self._config.image}", exc_info=True)

    @property
    def runtime(self) -> RemoteRuntime:
        """Returns the runtime if running.

        Raises:
            DeploymentNotStartedError: If the deployment was not started.
        """
        if self._runtime is None:
            raise DeploymentNotStartedError()
        return self._runtime
