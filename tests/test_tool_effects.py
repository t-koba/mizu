from dataclasses import replace
import json

from mizu import reviews
from mizu.config import load
from mizu.errors import ConfigError
from mizu.protocol import DEFINITIONS, finish_definition, tool_definitions
from support import Fixture


class ToolEffectTests(Fixture):
    @staticmethod
    def expected_finish(role, config):
        # A bound role only sees the typed verdict when a host config
        # authorizes its review seal; an unbound listing keeps the full manual.
        return finish_definition(role.finish_outcomes,
                                 review_verdict=config is not None and reviews.authorized(config, role),
                                 config=config, role=role)

    def test_appends_verbatim_only_to_the_named_tool(self):
        statement = '  Warm the café cache for "offline" builds.  '
        with self.file.open('a', encoding='utf-8') as stream:
            stream.write(f'\n[tool_effects]\nmaterialize = {json.dumps(statement, ensure_ascii=False)}\n'
                         'unknown_tool = "This operation is not advertised."\n')
        config = load(self.file)
        capabilities = ("materialize", "read")
        effect, other = tool_definitions(capabilities, config=config)
        base_effect, base_other = tool_definitions(capabilities)
        self.assertEqual(effect, {**base_effect, "description": base_effect["description"] + "\n\n" + statement})
        self.assertEqual(other, base_other)
        self.assertEqual(tool_definitions(("read",), config=config), [base_other])

    def test_empty_configuration_preserves_every_description(self):
        self.assertEqual(self.config.tool_effects, {})
        role = replace(self.config.roles["consult"], finish_outcomes=("done",))
        for config in (None, self.config):
            for tool in tool_definitions(tuple(DEFINITIONS), role=role, config=config):
                name = tool["operation"]
                description, schema = (self.expected_finish(role, config) if name == "finish"
                                       else DEFINITIONS[name])
                with self.subTest(configured=config is not None, name=name):
                    self.assertEqual(tool["description"].encode("utf-8"), description.encode("utf-8"))
                    self.assertEqual(tool["inputSchema"], schema)

    def test_appends_to_role_specific_finish_description(self):
        statement = "The operator uses this finish result."
        with self.file.open('a', encoding='utf-8') as stream:
            stream.write(f'\n[tool_effects]\nfinish = {json.dumps(statement)}\n')
        role = replace(self.config.roles["consult"], finish_outcomes=("done",))
        [finish] = tool_definitions(("finish",), role=role, config=load(self.file))
        description, schema = self.expected_finish(role, load(self.file))
        self.assertEqual(finish["description"], description + "\n\n" + statement)
        self.assertEqual(finish["inputSchema"], schema)

    def test_invalid_tool_effects_are_rejected_on_load(self):
        original = self.file.read_text(encoding='utf-8')
        invalid = ['tool_effects = []\n', 'tool_effects = 1\n',
                   'tool_effects = [{ materialize = "text" }]\n',
                   '[tool_effects]\n"" = "text"\n']
        invalid += [f'[tool_effects]\nmaterialize = {json.dumps(value)}\n'
                    for value in (False, [], {}, '', 'x' * 1025, '\0', '\t', '\n', '\x1b', '\x7f', '\x9b')]
        for snippet in invalid:
            with self.subTest(setting=snippet[:80]):
                self.file.write_text(snippet + '\n' + original, encoding='utf-8')
                with self.assertRaisesRegex(ConfigError, 'tool_effects'):
                    load(self.file)
