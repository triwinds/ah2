import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from Arknights.addons.contrib.material_recommendation import penguin, yituliu
from Arknights.addons.contrib.maa import maa_cli


def import_grass_on_aog():
    with open('cache/data_version.txt', 'rb') as f:
        data_version = f.read()
    response = SimpleNamespace(content=data_version, status_code=200)
    with patch('requests.get', return_value=response):
        return importlib.import_module('Arknights.addons.contrib.grass_on_aog')


class MaterialRecommendationTest(unittest.TestCase):
    def test_maa_fight_keeps_stage_when_cli_fails(self):
        command_error = maa_cli.MaaCommandError(
            ['fight', '--times', '1', '15-9'],
            1,
            'Fight Error: proxy button is locked',
        )

        with patch.object(maa_cli, 'inited', True), \
                patch.object(maa_cli, 'execute_maa_command', side_effect=command_error):
            with self.assertRaises(maa_cli.MaaFightError) as context:
                maa_cli.maa_fight('15-9', times=1, expiring_medicine=0)

        self.assertEqual(context.exception.stage_code, '15-9')
        self.assertIn('proxy button is locked', context.exception.output)

    def test_grass_falls_back_when_recommended_stage_cannot_be_proxied(self):
        grass_on_aog = import_grass_on_aog()
        command_error = maa_cli.MaaCommandError(['fight', '15-9'], 1, 'Fight Error')
        fight_error = maa_cli.MaaFightError('15-9', command_error)
        navigator = MagicMock()
        navigator.navigate_and_combat.side_effect = [fight_error, ('1-7', 0)]

        addon = object.__new__(grass_on_aog.GrassAddOn)
        addon.logger = MagicMock()
        addon.addon = MagicMock(return_value=navigator)
        with patch.object(addon, 'choose_stage', return_value='15-9'):
            result = addon.run()

        self.assertEqual(result, ('1-7', 0))
        self.assertEqual(
            navigator.navigate_and_combat.call_args_list,
            [call('15-9', 1000), call('1-7', 1000)],
        )

    def test_parse_yituliu_recommended_stage_list(self):
        payload = {
            'code': 200,
            'data': {
                'updateTime': '2026/05/31 10:00:00',
                'recommendedStageList': [
                    {
                        'itemType': '固源岩组',
                        'itemTypeId': '30013',
                        'stageResultList': [
                            {'stageCode': '4-6', 'stageEfficiency': 0.91},
                            {'stageCode': '1-7', 'stageEfficiency': 1.23, 'sampleSize': 12345},
                        ],
                    },
                ],
            },
        }

        result = yituliu.parse_yituliu_response(payload)

        self.assertEqual(result['固源岩组']['itemId'], '30013')
        self.assertEqual(result['固源岩组']['stageCode'], '1-7')
        self.assertEqual(result['固源岩组']['stageEfficiency'], 1.23)
        self.assertEqual(result['固源岩组']['source'], 'yituliu')

    def test_parse_yituliu_legacy_stage_list_picks_highest_efficiency(self):
        payload = {
            'data': [
                [
                    {'itemName': 'RMA70-12', 'itemId': '30103', 'stageCode': '2-10', 'stageEfficiency': 0.9},
                    {'itemName': 'RMA70-12', 'itemId': '30103', 'stageCode': '9-19', 'stageEfficiency': 1.1},
                ],
            ],
        }

        result = yituliu.parse_yituliu_response(payload)

        self.assertEqual(result['RMA70-12']['stageCode'], '9-19')

    def test_request_yituliu_data_uses_current_matrix_mirror(self):
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {'matrix': []},
            raise_for_status=lambda: None,
        )
        with patch.object(yituliu.requests, 'get', return_value=response) as request:
            result = yituliu.request_yituliu_data()

        self.assertEqual(result, {'matrix': []})
        request.assert_called_once_with(yituliu.MATRIX_ENDPOINT, timeout=yituliu.REQUEST_TIMEOUT)

    def test_parse_yituliu_matrix_response(self):
        matrix = {
            'matrix': [
                {'stageId': 'main_01-07', 'itemId': '30013', 'times': 1000, 'quantity': 500},
                {'stageId': 'main_04-06', 'itemId': '30013', 'times': 1000, 'quantity': 600},
            ],
        }
        t3_items = [{'itemId': '30013', 'name': '固源岩组', 'itemType': 'MATERIAL', 'rarity': 2}]
        stages = [
            {'stageId': 'main_01-07', 'code': '1-7', 'stageType': 'MAIN', 'apCost': 6},
            {'stageId': 'main_04-06', 'code': '4-6', 'stageType': 'MAIN', 'apCost': 18},
        ]

        result = yituliu._parse_matrix_response(
            matrix['matrix'], t3_items=t3_items, stages=stages
        )

        self.assertEqual(result['固源岩组']['stageCode'], '1-7')
        self.assertEqual(result['固源岩组']['source'], 'yituliu')

    def test_parse_yituliu_response_rejects_missing_data(self):
        with self.assertRaises(yituliu.RecommendationError):
            yituliu.parse_yituliu_response({'code': 200, 'data': {}})

    def test_build_penguin_matrix_recommendations_picks_lowest_ap_expect(self):
        t3_items = [
            {'itemId': '30013', 'name': '固源岩组'},
        ]
        stages = [
            {
                'stageId': 'main_01-07',
                'stageType': 'MAIN',
                'code': '1-7',
                'apCost': 6,
                'existence': {'CN': {'exist': True}},
            },
            {
                'stageId': 'main_04-06',
                'stageType': 'MAIN',
                'code': '4-6',
                'apCost': 18,
                'existence': {'CN': {'exist': True}},
            },
        ]
        matrix = [
            {'stageId': 'main_01-07', 'itemId': '30013', 'times': 1000, 'quantity': 500},
            {'stageId': 'main_04-06', 'itemId': '30013', 'times': 1000, 'quantity': 600},
        ]

        result = penguin.build_matrix_recommendations(t3_items, stages, matrix)

        self.assertEqual(result['固源岩组']['stageCode'], '1-7')
        self.assertEqual(result['固源岩组']['apExpect'], 12)
        self.assertEqual(result['固源岩组']['source'], 'penguin')

    def test_grass_falls_back_to_lowest_owned_activity_t3_stage(self):
        grass_on_aog = import_grass_on_aog()
        my_items = [
            {'name': '糖组', 'itemId': '30023', 'count': 2},
            {'name': '固源岩组', 'itemId': '30013', 'count': 5},
            {'name': '全新装置', 'itemId': '30063', 'count': 20},
        ]
        stage_code_map = {
            'EV-1': {
                'code': 'EV-1',
                'zoneId': 'act1_zone1',
                'stageDropInfo': {
                    'displayDetailRewards': [
                        {'type': 'MATERIAL', 'dropType': 'NORMAL', 'id': '30013'},
                    ],
                },
            },
            'EV-2': {
                'code': 'EV-2',
                'zoneId': 'act1_zone1',
                'stageDropInfo': {
                    'displayDetailRewards': [
                        {'type': 'MATERIAL', 'dropType': 'NORMAL', 'id': '30023'},
                    ],
                },
            },
        }

        with patch.object(grass_on_aog, 'get_available_activity_stages',
                          return_value=['EV-1', 'EV-2']), \
                patch.object(grass_on_aog, 'get_t3_item_ids',
                      return_value={'30013', '30023', '30063'}), \
                patch('Arknights.addons.contrib.activity.get_stage_map',
                      return_value=(stage_code_map, {})), \
                patch('Arknights.addons.contrib.activity.get_activity_info',
                      return_value={'startTime': 1}):
            stage = grass_on_aog.get_stage({}, my_items, prefer_activity=True)

        self.assertEqual(stage, 'EV-2')


if __name__ == '__main__':
    unittest.main()
