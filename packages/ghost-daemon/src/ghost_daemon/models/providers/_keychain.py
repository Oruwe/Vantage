from __future__ import annotations

import os

_SERVICE = "vantage"


def resolve_api_key(env_var: str) -> str:
    """Look up an API key: env var first, OS keychain second.

    Lookup order:
    1. Environment variable named ``env_var`` (e.g. XAI_API_KEY)
    2. OS keychain entry stored under service="vantage", username=``env_var``
       (Windows Credential Manager, macOS Keychain, or libsecret on Linux)

    Raises EnvironmentError with actionable instructions if neither is set.
    """
    value = os.environ.get(env_var, "").strip()
    if value:
        return value

    try:
        import keyring
        raw = keyring.get_password(_SERVICE, env_var)
        value = raw.strip() if raw else ""
    except Exception:
        value = ""

    if value:
        return value

    raise EnvironmentError(
        f"API key not found.\n\n"
        f"Option 1 — User environment variable (persists across sessions):\n"
        f"  PowerShell:  [System.Environment]::SetEnvironmentVariable('{env_var}', 'your-key', 'User')\n"
        f"  bash/zsh:    echo 'export {env_var}=your-key' >> ~/.profile\n\n"
        f"Option 2 — OS keychain (more secure, no plaintext file):\n"
        f"  vantage-ghost key set {env_var}\n"
    )


def store_api_key(env_var: str, value: str) -> None:
    """Store a key in the OS keychain under service='vantage'."""
    import keyring
    keyring.set_password(_SERVICE, env_var, value.strip())


def delete_api_key(env_var: str) -> None:
    import keyring
    try:
        keyring.delete_password(_SERVICE, env_var)
    except keyring.errors.PasswordDeleteError:
        pass
