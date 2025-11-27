import json
import docker
import tempfile
import time

def run_in_docker_sdk(code: str):
    """Using Docker SDK for better control and reliability"""
    client = docker.from_env()
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.py', encoding='utf-8') as f:
        f.write(code)
        temp_path = f.name
    
    try:
        start_time = time.perf_counter()
        
        # Create and run container
        container = client.containers.run(
            image='python:3.11-slim',
            command=f'python /app/code.py',
            volumes={temp_path: {'bind': '/app/code.py', 'mode': 'ro'}},
            network_mode='none',
            mem_limit='100m',
            cpu_period=100000,
            cpu_quota=50000,  # 50% CPU
            remove=True,  # Auto-remove after execution
            detach=False,  # Wait for completion
            stdout=True,
            stderr=True
        )
        
        end_time = time.perf_counter()
        
        # Parse output
        if isinstance(container, bytes):
            output = container.decode('utf-8')
        else:
            output = str(container)
        
        return {
            "stdout": output,
            "stderr": "",
            "exit_code": 0,
            "execution_time_seconds": end_time - start_time,
        }
        
    except docker.errors.ContainerError as e:
        end_time = time.perf_counter()
        return {
            "stdout": "",
            "stderr": str(e),
            "exit_code": e.exit_status,
            "execution_time_seconds": end_time - start_time,
        }
    except Exception as e:
        end_time = time.perf_counter()
        return {
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
            "execution_time_seconds": end_time - start_time,
            "error": f"Execution failed: {str(e)}"
        }
    finally:
        import os
        if os.path.exists(temp_path):
            os.remove(temp_path)

if __name__ == "__main__":
    sample_code = """
import os    
print("Hello from Docker!")
for i in range(5):
    print("i =", i)
"""

    print("Testing with Docker SDK...")
    result = run_in_docker_sdk(sample_code)
    print(json.dumps(result, indent=4, ensure_ascii=False))