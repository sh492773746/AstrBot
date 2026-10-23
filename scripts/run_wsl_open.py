"""Retired independent worker entry point; retained for explicit diagnostics."""


def main():
    """Refuse startup without loading configuration, credentials or networking."""
    raise SystemExit(
        "Independent Wangshangliao mode is retired. "
        "Use the integrated service in the AstrBot management page."
    )


if __name__ == "__main__":
    main()
