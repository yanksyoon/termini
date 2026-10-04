"""Deterministic role allocation.

Frozen rule (controller-api.md §4.1 / node-bootstrap.md §5.3):

    A node reporting >= 1 GPU with vram_bytes >= 12 GiB (12884901888 bytes) is
    assigned `inference`; otherwise `compute`. The `inference` role is capped
    to `max_inference_nodes` per project (default 1 — node C in the demo):
    the first qualifying GPU node wins; later qualifying GPU nodes fall back
    to `compute` unless project config raises the cap.

Determinism: given the same registered node set in the same registration order
(dedup via `machine_id`), allocation is stable and reproducible. The rule is a
pure function of (capabilities, current inference count, project cap).
"""
from __future__ import annotations

DEFAULT_GPU_VRAM_MIN_BYTES = 12 * 1024 ** 3  # 12 GiB


def assign_role(
    gpu_vram_bytes: int,
    inference_count: int,
    max_inference_nodes: int,
    gpu_vram_min_bytes: int = DEFAULT_GPU_VRAM_MIN_BYTES,
) -> str:
    """Return 'inference' or 'compute' for a node's GPU capability.

    `gpu_vram_bytes` is the largest VRAM reported by any GPU on the node
    (0 when no GPU is present).
    """
    qualifies = gpu_vram_bytes >= gpu_vram_min_bytes
    if qualifies and inference_count < max_inference_nodes:
        return "inference"
    return "compute"
