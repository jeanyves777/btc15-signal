from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    return values


def main() -> None:
    print("Kalshi keys stay on this computer. Never paste the private key into Telegram.")
    api_key_id = input("Kalshi API key ID: ").strip()
    private_key_path = Path(input("Absolute path to downloaded Kalshi .key file: ").strip())
    if not api_key_id:
        raise SystemExit("API key ID is required")
    if not private_key_path.is_absolute() or not private_key_path.is_file():
        raise SystemExit("Private key path must be an existing absolute file path")
    confirmation = input("Type ENABLE to allow Telegram-authorized live orders: ").strip()
    values = load_env(ENV_PATH)
    values["KALSHI_API_KEY_ID"] = api_key_id
    values["KALSHI_PRIVATE_KEY_PATH"] = str(private_key_path)
    values["EXECUTION_ENABLED"] = "true" if confirmation == "ENABLE" else "false"
    temporary = ENV_PATH.with_suffix(".tmp")
    temporary.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    temporary.chmod(0o600)
    temporary.replace(ENV_PATH)
    ENV_PATH.chmod(0o600)
    print(f"Saved locally. Live execution: {values['EXECUTION_ENABLED']}")


if __name__ == "__main__":
    main()
