"""Shared extension objects, created once and bound in create_app()."""
from argon2 import PasswordHasher
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()

# ── Argon2id hasher — Pi-tuned parameters ────────────────────────────────────
# 64 MB memory cost + 3 time iterations: ~200-400 ms on Pi 4, memory-hard
# enough to cripple GPU/ASIC offline attacks.
password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)
