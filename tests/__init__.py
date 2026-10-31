"""Test package: never load the live /etc/tripwire/secrets.env."""

import os
from pathlib import Path

os.environ["TRIPWIRE_SECRETS"] = str(Path(__file__).with_name("secrets.test.env"))
# Never call live translation providers from the test suite; tests opt in explicitly.
os.environ["TRIPWIRE_TRANSLATE"] = "0"
