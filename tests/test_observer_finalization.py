"""An interrupted observer finish must leave its source eligible for retry."""
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.errors import Busy
from mizu.fs import read_json
from mizu.runtime import Engine


