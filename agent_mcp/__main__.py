# Load environment variables as the very first thing
import os
from pathlib import Path
from dotenv import load_dotenv

# Find and load .env file from project root
current_file = Path(__file__).resolve()
project_root = current_file.parent.parent  # Go up to Agent-MCP directory
env_file = project_root / '.env'

print(f"Looking for .env at: {env_file}")
if env_file.exists():
    print(f"Loading .env from: {env_file}")
    load_dotenv(dotenv_path=str(env_file))
    # VULN-002: never log a prefix of OPENAI_API_KEY — the journal /
    # stdout trust boundary is wider than the in-process secret store,
    # so even a 20-char head is enough to fingerprint the org's key.
    # Presence-only check keeps the operator signal without the leak.
    _has_key = bool(os.environ.get('OPENAI_API_KEY'))
    print(f"OPENAI_API_KEY in environment: {'present' if _has_key else 'NOT FOUND'}")
else:
    print(f"No .env file found at {env_file}")
    load_dotenv()  # Try default locations

# Now import and run the CLI
from .cli import main_cli

if __name__ == "__main__":
    main_cli()