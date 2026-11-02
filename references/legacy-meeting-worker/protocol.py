# Historical reference only. Not integrated or supported for deployment. See README.md.
import dataclasses
from typing import List, Optional, Dict, Any, Union
from datetime import datetime

@dataclasses.dataclass
class Envelope:
    protocol_version: str
    request_id: str
    worker_id: str
    site_id: str
    sent_at: str

@dataclasses.dataclass
class InstanceConfig:
    instance_id: str
    agent_id: str
    adapter: str
    route_key: str
    capabilities: List[str]

@dataclasses.dataclass
class RegisterRequest(Envelope):
    worker_version: str
    instances: List[InstanceConfig]

@dataclasses.dataclass
class RegisterResponse:
    protocol_version: str
    server_time: str
    registration_id: str
    heartbeat_interval_seconds: int
    lease_duration_seconds: int
    accepted_instance_ids: List[str]
    rejected_instances: List[str]

@dataclasses.dataclass
class InstanceStatus:
    instance_id: str
    status: str
    detail: Optional[str]

@dataclasses.dataclass
class HeartbeatRequest(Envelope):
    status: str
    active_task_ids: List[str]
    instances: List[InstanceStatus]

@dataclasses.dataclass
class HeartbeatResponse:
    protocol_version: str
    server_time: str
    accepted: bool
    next_heartbeat_seconds: int
    drain: bool

@dataclasses.dataclass
class LeaseRequest(Envelope):
    available_instance_ids: List[str]
    max_tasks: int
    wait_seconds: int

@dataclasses.dataclass
class Task:
    task_id: str
    lease_id: str
    lease_expires_at: str
    attempt: int
    instance_id: str
    agent_id: str
    adapter: str
    route_key: str
    session_id: str
    roundtable_id: Optional[str]
    stage_key: str
    context_generation: int
    prompt: str
    timeout_seconds: int
    max_output_chars: int
    permission_policy: str

@dataclasses.dataclass
class LeaseResponse:
    protocol_version: str
    server_time: str
    task: Optional[Task] = None

@dataclasses.dataclass
class TaskStartedRequest(Envelope):
    lease_id: str
    started_at: str

@dataclasses.dataclass
class TaskCompletedRequest(Envelope):
    lease_id: str
    completion_id: str
    completed_at: str
    instance_id: str
    context_generation: int
    body: str
    provider_session_ref: Optional[str]
    diagnostics: Dict[str, Any]

@dataclasses.dataclass
class TaskCompletedResponse:
    protocol_version: str
    accepted: bool
    duplicate: bool
    turn_id: Optional[str] = None

@dataclasses.dataclass
class TaskFailedRequest(Envelope):
    lease_id: str
    failure_id: str
    failed_at: str
    category: str
    retryable: bool
    message: str
    diagnostics: Dict[str, Any]

@dataclasses.dataclass
class ErrorEnvelope:
    protocol_version: str
    error: Dict[str, Any]
