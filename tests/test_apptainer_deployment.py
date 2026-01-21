"""Tests for Apptainer deployment.

These tests require Apptainer to be installed on the system.
They will be skipped if Apptainer is not available.
"""
import subprocess

import pytest

from swerex.deployment.config import DockerDeploymentConfig
from swerex.deployment.docker import DockerDeployment
from swerex.utils.free_port import find_free_port


def is_apptainer_available():
    """Check if Apptainer is installed and available."""
    try:
        subprocess.check_output(["apptainer", "--version"], stderr=subprocess.DEVNULL)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


pytestmark = pytest.mark.skipif(
    not is_apptainer_available(),
    reason="Apptainer not available"
)


@pytest.mark.slow
async def test_apptainer_deployment():
    """Test basic Apptainer deployment with pre-pulled image."""
    port = find_free_port()
    print(f"Using port {port} for the apptainer deployment")
    
    # Use a simple image for testing
    d = DockerDeployment(
        image="python:3.11",
        port=port,
        container_runtime="apptainer",
        pull="missing"
    )
    
    with pytest.raises(RuntimeError):
        await d.is_alive()
    
    await d.start()
    assert await d.is_alive()
    await d.stop()


@pytest.mark.slow
async def test_apptainer_deployment_with_docker_uri():
    """Test Apptainer deployment with explicit docker:// URI."""
    port = find_free_port()
    print(f"Using port {port} for the apptainer deployment")
    
    d = DockerDeployment(
        image="docker://python:3.11-slim",
        port=port,
        container_runtime="apptainer",
        pull="missing"
    )
    
    await d.start()
    assert await d.is_alive()
    await d.stop()


@pytest.mark.slow
async def test_apptainer_deployment_with_sif_cache():
    """Test Apptainer deployment with custom SIF cache directory."""
    import tempfile
    from pathlib import Path
    
    port = find_free_port()
    print(f"Using port {port} for the apptainer deployment")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        d = DockerDeployment(
            image="python:3.11-slim",
            port=port,
            container_runtime="apptainer",
            apptainer_sif_cache_dir=tmpdir,
            pull="missing"
        )
        
        await d.start()
        assert await d.is_alive()
        
        # Check that .sif file was created
        sif_files = list(Path(tmpdir).glob("*.sif"))
        assert len(sif_files) > 0, "No .sif file created in cache directory"
        
        await d.stop()


def test_apptainer_deployment_config():
    """Test that DockerDeployment config works with apptainer."""
    config = DockerDeploymentConfig(
        image="test:latest",
        container_runtime="apptainer",
        port=8080,
        pull="never"
    )
    
    deployment = DockerDeployment.from_config(config)
    assert deployment._config.container_runtime == "apptainer"
    assert deployment._config.image == "test:latest"
    assert deployment._config.port == 8080


def test_apptainer_config_validation():
    """Test configuration validation for Apptainer runtime."""
    # Test default container runtime is docker
    config = DockerDeploymentConfig(image="test")
    assert config.container_runtime == "docker"
    
    # Test setting container runtime to apptainer
    config = DockerDeploymentConfig(image="test", container_runtime="apptainer")
    assert config.container_runtime == "apptainer"
    
    # Test with SIF cache dir
    config = DockerDeploymentConfig(
        image="test",
        container_runtime="apptainer",
        apptainer_sif_cache_dir="/tmp/apptainer-cache"
    )
    assert config.apptainer_sif_cache_dir == "/tmp/apptainer-cache"


@pytest.mark.slow
async def test_apptainer_deployment_commands():
    """Test that commands can be executed in Apptainer deployment."""
    port = find_free_port()
    print(f"Using port {port} for the apptainer deployment")
    
    d = DockerDeployment(
        image="python:3.11-slim",
        port=port,
        container_runtime="apptainer",
        pull="missing"
    )
    
    await d.start()
    runtime = d.runtime
    
    # Test simple command execution
    from swerex.runtime.abstract import Command
    result = await runtime.execute(Command(command=["echo", "Hello from Apptainer"]))
    assert result.exit_code == 0
    assert "Hello from Apptainer" in result.stdout
    
    # Test bash session
    from swerex.runtime.abstract import BashAction, CreateBashSessionRequest

    await runtime.create_session(CreateBashSessionRequest())
    
    response = await runtime.run_in_session(BashAction(command="export TEST_VAR='apptainer_test'"))
    assert response.exit_code == 0
    
    response = await runtime.run_in_session(BashAction(command="echo $TEST_VAR"))
    assert response.exit_code == 0
    assert "apptainer_test" in response.output
    
    await d.stop()


@pytest.mark.slow
@pytest.mark.skipif(
    not is_apptainer_available(),
    reason="Apptainer not available or Docker not available for building"
)
async def test_apptainer_deployment_with_python_standalone():
    """Test Apptainer deployment with python_standalone_dir.
    
    Note: This requires Docker to be available for the initial build step.
    """
    try:
        subprocess.check_output(["docker", "--version"], stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("Docker not available for building standalone Python")
    
    port = find_free_port()
    print(f"Using port {port} for the apptainer deployment with python standalone")
    
    d = DockerDeployment(
        image="ubuntu:latest",
        port=port,
        container_runtime="apptainer",
        python_standalone_dir="/root",
        pull="missing"
    )
    
    await d.start()
    assert await d.is_alive()
    await d.stop()


def test_apptainer_sif_path_generation():
    """Test SIF file path generation."""
    config = DockerDeploymentConfig(
        image="ubuntu:latest",
        container_runtime="apptainer"
    )
    
    deployment = DockerDeployment.from_config(config)
    sif_path = deployment._get_sif_image_path("ubuntu:latest")
    
    assert sif_path.endswith(".sif")
    assert "ubuntu" in sif_path
    
    # Test with docker:// prefix
    sif_path2 = deployment._get_sif_image_path("docker://ubuntu:latest")
    assert sif_path2.endswith(".sif")
    
    # Test with cache dir
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        config2 = DockerDeploymentConfig(
            image="ubuntu:latest",
            container_runtime="apptainer",
            apptainer_sif_cache_dir=tmpdir
        )
        deployment2 = DockerDeployment.from_config(config2)
        sif_path3 = deployment2._get_sif_image_path("ubuntu:latest")
        assert tmpdir in sif_path3


@pytest.mark.slow
async def test_apptainer_deployment_pull_policies():
    """Test different pull policies with Apptainer."""
    import tempfile
    from pathlib import Path
    
    port = find_free_port()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Test "missing" - should pull if not present
        d1 = DockerDeployment(
            image="python:3.11-slim",
            port=port,
            container_runtime="apptainer",
            apptainer_sif_cache_dir=tmpdir,
            pull="missing"
        )
        
        await d1.start()
        assert await d1.is_alive()
        await d1.stop()
        
        # Verify .sif exists
        sif_files = list(Path(tmpdir).glob("*.sif"))
        assert len(sif_files) > 0
        
        # Test "never" - should fail if not present (but we just created it)
        d2 = DockerDeployment(
            image="python:3.11-slim",
            port=find_free_port(),
            container_runtime="apptainer",
            apptainer_sif_cache_dir=tmpdir,
            pull="never"
        )
        
        await d2.start()
        assert await d2.is_alive()
        await d2.stop()
