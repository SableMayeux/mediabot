#!/usr/bin/env python3
"""Host GPU admission/preemption. Controls only the labelled local-ai-ollama."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

CONTAINER = 'local-ai-ollama'
LABEL = 'io.sable.local-ai.runtime'


def run(args, timeout=5):
    return subprocess.check_output(args, text=True, timeout=timeout).strip()


def classify(gpu, foreign, ram_available):
    free, utilization, encoder, decoder, temperature = gpu
    healthy = not foreign and encoder == 0 and decoder == 0 and free >= 4096 and temperature < 80 and ram_available >= 6144
    reason = ('foreign_gpu_workload' if foreign or encoder or decoder else
              'gpu_vram_low' if free < 4096 else 'gpu_temperature' if temperature >= 80 else
              'host_memory_low' if ram_available < 6144 else 'ready')
    return {'healthy': healthy, 'admit': healthy and free >= 6144 and utilization <= 10 and ram_available >= 8192,
            'reason': reason, 'gpu_free_mib': free, 'gpu_utilization_percent': utilization,
            'encoder_percent': encoder, 'decoder_percent': decoder, 'gpu_temperature_c': temperature,
            'host_available_mib': ram_available, 'foreign_gpu_process_count': len(foreign)}


def runtime():
    data = json.loads(run(['docker', 'inspect', CONTAINER]))[0]
    if data['Config'].get('Labels', {}).get(LABEL) != '1':
        raise RuntimeError('runtime_label_mismatch')
    return data


def gpu_compute_pids():
    raw = run(['nvidia-smi', '--id=0', '--query-compute-apps=pid', '--format=csv,noheader,nounits'])
    return {int(line.strip()) for line in raw.splitlines() if line.strip()}


def gpu_pid_is_owned(pid, identity):
    # Current cgroup membership avoids caching ownership across process/PID reuse.
    groups = Path('/proc', str(pid), 'cgroup').read_text()
    for line in groups.splitlines():
        path = line.split(':', 2)[2]
        if any(component in (identity, 'docker-' + identity + '.scope') for component in path.split('/')):
            return True
    return False


def foreign_compute_pids(identity):
    foreign, vanished = set(), set()
    for pid in gpu_compute_pids():
        try:
            if not gpu_pid_is_owned(pid, identity):
                foreign.add(pid)
        except FileNotFoundError:
            vanished.add(pid)
    if vanished:
        # NVML may briefly retain an exited runner. Re-query; never trust an old own-PID list.
        for pid in vanished & gpu_compute_pids():
            try:
                if not gpu_pid_is_owned(pid, identity):
                    foreign.add(pid)
            except FileNotFoundError as error:
                raise RuntimeError('unresolved_gpu_process') from error
    return foreign

def collect():
    data = runtime()
    running = data['State']['Running']

    gpu = [int(x.strip()) for x in run(['nvidia-smi', '--id=0', '--query-gpu=memory.free,utilization.gpu,utilization.encoder,utilization.decoder,temperature.gpu', '--format=csv,noheader,nounits']).split(',')]
    foreign = foreign_compute_pids(data['Id'])

    memory = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    result = classify(gpu, foreign, memory['MemAvailable']//1024)
    ready = data['State'].get('Health', {}).get('Status') == 'healthy'
    result.update(runtime_running=running, runtime_ready=ready, runtime_id=data['Id'])
    # Recovery scheduling still uses idle resources while a stopped runtime has no health.
    result['resources_admit'] = result['admit']
    result['admit'] = result['admit'] and ready
    return result


def atomic_json(directory, name, data, mode=0o644, durable=False):
    fd, temporary = tempfile.mkstemp(prefix='.'+name+'.', dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.write('\n')
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, directory/name)
        if durable:
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def owns_stop(directory, identity):
    try:
        marker = json.loads((directory/'owned-stop.json').read_text())
        return marker == {'container': CONTAINER, 'container_id': identity, 'owned_stop': True}
    except (OSError, ValueError):
        return False


def mark_owned_stop(directory, identity):
    atomic_json(directory, 'owned-stop.json', {'container': CONTAINER, 'container_id': identity, 'owned_stop': True}, 0o600, True)


def clear_owned_stop(directory, identity):
    if owns_stop(directory, identity):
        (directory/'owned-stop.json').unlink()
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def write_state(directory, state):
    atomic_json(directory, 'status.json', {'observed_at': time.time(), **state})


def stop_signal(_signum, _frame):
    raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--manage', action='store_true', help='Stop labelled AI on contention; recover only owned stops after 10s idle.')
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop_signal)
    args.state_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    safe_since = None
    try:
        while True:
            try:
                state = collect()
                owned = owns_stop(args.state_dir, state['runtime_id'])
                if not state['healthy']:
                    write_state(args.state_dir, state)
                    safe_since = None
                    if args.manage and state['runtime_running']:
                        # Persist before stop, so a watcher crash cannot lose recovery ownership.
                        mark_owned_stop(args.state_dir, state['runtime_id'])
                        subprocess.run(['docker', 'stop', '-t', '2', CONTAINER], check=True, capture_output=True, timeout=8)
                        state['runtime_running'] = False
                elif owned and not state['runtime_running'] and state['resources_admit']:
                    safe_since = safe_since or time.monotonic()
                    if args.manage and time.monotonic()-safe_since >= 10:
                        subprocess.run(['docker', 'start', CONTAINER], check=True, capture_output=True, timeout=10)
                        clear_owned_stop(args.state_dir, state['runtime_id'])
                        state['runtime_running'] = True
                        safe_since = None
                else:
                    safe_since = None
                    if args.manage and owned and state['runtime_running']:
                        clear_owned_stop(args.state_dir, state['runtime_id'])
                if not state['runtime_running']:
                    state['admit'] = False
            except Exception:
                state = {'healthy': False, 'admit': False, 'reason': 'gpu_monitor_failed'}
                write_state(args.state_dir, state)
                safe_since = None
                if args.manage:
                    try:
                        data = runtime()
                        if data['State']['Running']:
                            mark_owned_stop(args.state_dir, data['Id'])
                            subprocess.run(['docker', 'stop', '-t', '2', CONTAINER], check=True, capture_output=True, timeout=8)
                    except Exception:
                        pass
            write_state(args.state_dir, state)
            time.sleep(0.5)
    finally:
        write_state(args.state_dir, {'healthy': False, 'admit': False, 'reason': 'gpu_monitor_stopped'})


if __name__ == '__main__':
    main()