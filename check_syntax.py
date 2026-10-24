import ast

files = [
    r"src\vantage\schemas.py",
    r"packages\ghost-daemon\src\ghost_daemon\config.py",
    r"packages\ghost-daemon\src\ghost_daemon\approval\policy.py",
    r"packages\ghost-daemon\src\ghost_daemon\agents\probe_agent.py",
    r"packages\ghost-daemon\src\ghost_daemon\transport\ws_client.py",
    r"packages\ghost-daemon\src\ghost_daemon\daemon.py",
    r"packages\ghost-daemon\src\ghost_daemon\__main__.py",
    r"packages\ghost-daemon\src\ghost_daemon\storage\sqlite_store.py",
    # M5 — ghost-daemon
    r"packages\ghost-daemon\src\ghost_daemon\crypto.py",
    r"packages\ghost-daemon\src\ghost_daemon\subscribers\registry.py",
    # M5 — shadow-replica
    r"packages\shadow-replica\src\shadow_replica\config.py",
    r"packages\shadow-replica\src\shadow_replica\__main__.py",
    r"packages\shadow-replica\src\shadow_replica\connections\ws_hub.py",
    r"packages\shadow-replica\src\shadow_replica\resilience\fallback.py",
    r"packages\shadow-replica\src\shadow_replica\api\probe.py",
    r"packages\shadow-replica\src\shadow_replica\api\receive.py",
    r"packages\shadow-replica\src\shadow_replica\api\dashboard.py",
    r"packages\shadow-replica\src\shadow_replica\storage\received_store.py",
    r"packages\shadow-replica\src\shadow_replica\metrics.py",
    r"packages\shadow-replica\src\shadow_replica\agent\subscriber_agent.py",
    r"packages\shadow-replica\src\shadow_replica\app.py",
]

all_ok = True
for f in files:
    try:
        ast.parse(open(f, encoding="utf-8").read())
        print(f"OK   {f}")
    except SyntaxError as e:
        print(f"FAIL {f}  →  {e}")
        all_ok = False

print("\nAll OK" if all_ok else "\nSome files failed.")
