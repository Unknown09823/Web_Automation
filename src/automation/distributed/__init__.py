"""Distributed coordination across multiple nodes."""
from automation.distributed.coordinator import Coordinator, Worker, Assignment
from automation.distributed.worker import WorkerNode

__all__ = ["Coordinator", "Worker", "Assignment", "WorkerNode"]
