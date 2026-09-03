import unittest
from unittest.mock import MagicMock, patch

from Arknights.addons.stage_navigator import StageNavigator


class StageNavigatorTest(unittest.TestCase):
    def make_navigator(self):
        navigator = object.__new__(StageNavigator)
        combat = MagicMock()
        navigator.addon = MagicMock(return_value=combat)
        navigator.is_stage_supported = MagicMock(return_value=True)
        navigator.goto_stage = MagicMock()
        navigator.goto_latest_stage = MagicMock()
        navigator.logger = MagicMock()
        return navigator, combat

    def test_linux_builtin_stage_is_navigated_by_maa(self):
        navigator, combat = self.make_navigator()
        combat.combat_on_current_stage.return_value = ('PR-D-1', 0)

        with patch('Arknights.addons.stage_navigator.os.name', 'posix'):
            result = navigator.navigate_and_combat('pr-d-1', 1000)

        self.assertEqual(result, ('PR-D-1', 0))
        navigator.goto_stage.assert_not_called()
        combat.combat_on_current_stage.assert_called_once_with(1000, 'PR-D-1')

    def test_linux_activity_stage_is_navigated_by_ah2(self):
        navigator, combat = self.make_navigator()
        combat.combat_on_current_stage.return_value = ('EV-1', 0)

        with patch('Arknights.addons.stage_navigator.os.name', 'posix'):
            result = navigator.navigate_and_combat('ev-1', 1000)

        self.assertEqual(result, ('EV-1', 0))
        navigator.goto_stage.assert_called_once_with('EV-1')
        combat.combat_on_current_stage.assert_called_once_with(1000)


if __name__ == '__main__':
    unittest.main()
