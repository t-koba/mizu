"""Session selection lives in unit_prompts; runtime re-exports the same object."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mizu.runtime as runtime
import mizu.unit_prompts as unit_prompts


class SessionPromptOwnerTests(unittest.TestCase):
    def test_runtime_reexports_the_owner_function(self):
        self.assertIs(runtime.session_prompt, unit_prompts.session_prompt)
        self.assertEqual(unit_prompts.session_prompt.__module__, "mizu.unit_prompts")

    def test_ephemeral_context_uses_full_prompt_without_a_session(self):
        context = SimpleNamespace(ephemeral=True)
        with patch.object(unit_prompts, "prompt_for", return_value="prompt") as build:
            result = unit_prompts.session_prompt(context, object(), None)
        build.assert_called_once_with(context)
        self.assertEqual(result, ("prompt", "full", "", None, None))


if __name__ == "__main__":
    unittest.main()
