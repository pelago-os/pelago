# Historical reference only. Not integrated or supported for deployment. See README.md.
import json
import os
import time
import uuid
import argparse
import subprocess
from datetime import datetime, timezone
from client import CoordinatorClient
from adapters.fake_adapter import run_fake_adapter
from adapters.hermes_adapter import run_hermes_adapter
from adapters.openclaw_adapter import run_openclaw_adapter
from adapters.antigravity_adapter import run_antigravity_adapter

def get_utc_now_str():
    return datetime.now(timezone.utc).isoformat()

class Worker:
    def __init__(self, config_dir=None, tls_verify=True):
        if not config_dir:
            config_dir = os.path.expanduser("~/.example-meeting-worker/config")

        with open(os.path.join(config_dir, "credentials.json")) as f:
            self.creds = json.load(f)
        with open(os.path.join(config_dir, "registry.json")) as f:
            self.registry = json.load(f)

        self.client = CoordinatorClient(self.creds["coordinator_url"], self.creds["site_token"], tls_verify=tls_verify)
        self.worker_id = self.creds["worker_id"]
        self.site_id = self.creds["site_id"]
        self.heartbeat_interval = 20

    def _envelope(self):
        return {
            "protocol_version": "1",
            "request_id": f"req_{uuid.uuid4().hex[:8]}",
            "worker_id": self.worker_id,
            "site_id": self.site_id,
            "sent_at": get_utc_now_str()
        }

    def register(self):
        data = self._envelope()
        data.update({
            "worker_version": "0.1.0",
            "instances": self.registry["instances"]
        })
        res = self.client.register(data)
        if res and "heartbeat_interval_seconds" in res:
            self.heartbeat_interval = res["heartbeat_interval_seconds"]
        return res

    def heartbeat(self):
        data = self._envelope()
        data.update({
            "status": "idle",
            "active_task_ids": [],
            "instances": [{"instance_id": i["instance_id"], "status": "available", "detail": None} for i in self.registry["instances"]]
        })
        return self.client.heartbeat(data)

    def lease_task(self, instance_id=None):
        data = self._envelope()
        available = [i["instance_id"] for i in self.registry["instances"]]
        if instance_id and instance_id in available:
            available = [instance_id]
        data.update({
            "available_instance_ids": available,
            "max_tasks": 1,
            "wait_seconds": 0
        })
        res = self.client.lease(data)
        if res and "task" in res:
            return res["task"]
        return None

    def start_task(self, task):
        data = self._envelope()
        data.update({
            "lease_id": task["lease_id"],
            "started_at": get_utc_now_str()
        })
        self.client.task_started(task["task_id"], data)

    def complete_task(self, task, body, provider_session_ref=None, diagnostics=None):
        if diagnostics is None:
            max_chars = task.get("max_output_chars", 2000)
            truncated = len(body) > max_chars
            if truncated:
                body = body[:max_chars]
            diagnostics = {"duration_ms": 100, "exit_code": 0, "output_truncated": truncated}

        data = self._envelope()
        data.update({
            "lease_id": task["lease_id"],
            "completion_id": f"comp_{task['task_id']}_{task['attempt']}",
            "completed_at": get_utc_now_str(),
            "instance_id": task["instance_id"],
            "context_generation": task["context_generation"],
            "body": body,
            "provider_session_ref": provider_session_ref,
            "diagnostics": diagnostics
        })
        return self.client.task_complete(task["task_id"], data)

    def fail_task(self, task, category, message):
        msg = str(message)[:2000]
        data = self._envelope()
        data.update({
            "lease_id": task["lease_id"],
            "failure_id": f"fail_{task['task_id']}_{task['attempt']}",
            "failed_at": get_utc_now_str(),
            "category": category,
            "retryable": True,
            "message": msg,
            "diagnostics": {"duration_ms": 100, "exit_code": 1, "output_truncated": False}
        })
        self.client.task_fail(task["task_id"], data)

    def process_one_task(self, instance_id=None):
        task = self.lease_task(instance_id)
        if not task:
            return None

        # Route & Policy Enforcement
        allowed = False
        for inst in self.registry["instances"]:
            if (inst["instance_id"] == task["instance_id"] and
                inst["agent_id"] == task["agent_id"] and
                inst["adapter"] == task["adapter"] and
                inst["route_key"] == task["route_key"]):
                allowed = True
                break

        if not allowed or task.get("permission_policy") != "deny_interactive":
            self.fail_task(task, "invalid_task", "Task route mismatch or bad permission policy.")
            return None

        self.start_task(task)

        if task["adapter"] == "fake":
            try:
                if "THROW_TIMEOUT" in task["prompt"]:
                    raise TimeoutError("Adapter timed out")
                if "THROW_ERROR" in task["prompt"]:
                    raise Exception("Adapter crashed")
                output = run_fake_adapter(task["prompt"], task["timeout_seconds"])
                return self.complete_task(task, output)
            except TimeoutError as e:
                self.fail_task(task, "timeout", str(e))
                return None
            except Exception as e:
                self.fail_task(task, "adapter_error", str(e))
                return None

        if task["adapter"] == "hermes":
            try:
                result = run_hermes_adapter(task)
                return self.complete_task(
                    task,
                    result["body"],
                    provider_session_ref=result["provider_session_ref"],
                    diagnostics={
                        "duration_ms": result["duration_ms"],
                        "exit_code": result["exit_code"],
                        "output_truncated": result["output_truncated"],
                    }
                )
            except subprocess.TimeoutExpired as e:
                self.fail_task(task, "timeout", str(e))
                return None
            except ValueError as e:
                self.fail_task(task, "invalid_task", str(e))
                return None
            except Exception as e:
                self.fail_task(task, "adapter_error", str(e))
                return None

        if task["adapter"] == "openclaw":
            try:
                result = run_openclaw_adapter(task)
                return self.complete_task(
                    task,
                    result["body"],
                    provider_session_ref=result["provider_session_ref"],
                    diagnostics={
                        "duration_ms": result["duration_ms"],
                        "exit_code": result["exit_code"],
                        "output_truncated": result["output_truncated"],
                    }
                )
            except subprocess.TimeoutExpired as e:
                self.fail_task(task, "timeout", str(e))
                return None
            except ValueError as e:
                self.fail_task(task, "invalid_task", str(e))
                return None
            except Exception as e:
                self.fail_task(task, "adapter_error", str(e))
                return None

        if task["adapter"] == "antigravity":
            try:
                result = run_antigravity_adapter(task)
                return self.complete_task(
                    task,
                    result["body"],
                    provider_session_ref=result["provider_session_ref"],
                    diagnostics={
                        "duration_ms": result["duration_ms"],
                        "exit_code": result["exit_code"],
                        "output_truncated": result["output_truncated"],
                    }
                )
            except subprocess.TimeoutExpired as e:
                self.fail_task(task, "timeout", str(e))
                return None
            except ValueError as e:
                self.fail_task(task, "invalid_task", str(e))
                return None
            except Exception as e:
                self.fail_task(task, "adapter_error", str(e))
                return None

        self.fail_task(task, "adapter_error", f"Unknown adapter {task['adapter']}")
        return None

    def run_active_window(self, instance_id: str):
        self.register()
        next_heartbeat_at = time.time()

        while True:
            now = time.time()
            if now >= next_heartbeat_at:
                self.heartbeat()
                next_heartbeat_at = now + self.heartbeat_interval

            windows_info = self.client.get_windows(instance_id)
            if not windows_info.get("active", False):
                print(f"Window inactive. Reason: {windows_info.get('exit_reason', 'closed')}. Exiting clean shutdown.")
                break

            poll_interval = windows_info.get("poll_interval_seconds", 10)

            # Process task
            task_res = self.process_one_task(instance_id)
            if task_res is not None:
                # immediately check again
                continue

            time.sleep(poll_interval)

if __name__ == "__main__":
    def str2bool(v):
        if isinstance(v, bool):
            return v
        if v.lower() in ('yes', 'true', 't', 'y', '1'):
            return True
        elif v.lower() in ('no', 'false', 'f', 'n', '0'):
            return False
        else:
            import argparse
            raise argparse.ArgumentTypeError('Boolean value expected.')

    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Legacy mode alias. Equivalent to --mode once.")
    parser.add_argument("--mode", type=str, default="once", choices=["once", "active-window"], help="Worker execution mode.")
    parser.add_argument("--instance", "--instance-id", dest="instance", type=str, default=None, help="Instance ID to poll window for (required for active-window mode).")
    parser.add_argument("--tls-verify", type=str2bool, default=True, help="Verify TLS certs. Disable for local proxy dev.")
    parser.add_argument("--config-dir", type=str, default=None)
    args = parser.parse_args()

    mode = "once"
    if args.once:
        mode = "once"
    else:
        mode = args.mode

    w = Worker(config_dir=args.config_dir, tls_verify=args.tls_verify)

    if mode == "once":
        w.register()
        w.heartbeat()
        w.process_one_task()
        print("Worker completed --once execution.")
    elif mode == "active-window":
        if not args.instance:
            print("Error: --instance is required for --mode active-window")
            exit(1)
        print(f"Worker starting active-window loop for instance {args.instance}...")
        w.run_active_window(args.instance)
        print("Worker completed active-window execution.")
